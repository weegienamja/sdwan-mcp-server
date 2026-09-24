"""Guarded change plans and verification without live execution."""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast
from uuid import uuid4

from cryptography.fernet import InvalidToken
from pydantic import BaseModel, ConfigDict, Field

from cisco_vmanage_mcp.services.governance import AuthorizationError, GovernanceActor
from cisco_vmanage_mcp.services.investigations import sanitize_investigation_data
from cisco_vmanage_mcp.services.persistence import (
    canonical_json_bytes,
    canonical_sha256,
    load_or_create_fernet,
    prepare_private_directory,
    private_sqlite_connection,
)
from cisco_vmanage_mcp.services.snapshots import (
    FabricSnapshot,
    SnapshotComparison,
    compare_snapshots,
)

CHANGE_PLAN_SCHEMA_VERSION = 1
DEFAULT_MAX_PLANS = 500

ChangeOperation = Literal["attach-device-template", "activate-central-policy"]
PlanStatus = Literal[
    "planned",
    "approved",
    "verification-passed",
    "verification-failed",
    "cancelled",
    "expired",
]


class ExecutionDisabledError(RuntimeError):
    """Raised whenever live change execution is requested."""


class ChangePlanStoreError(RuntimeError):
    """Raised when encrypted change-plan state is unavailable or corrupt."""


class ChangeApproval(BaseModel):
    """Short-lived human approval bound to an exact change hash."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    actor_id: str
    approved_at: datetime
    expires_at: datetime
    plan_hash: str


class ChangeVerification(BaseModel):
    """Post-change comparison and rollback decision support."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    actor_id: str
    verified_at: datetime
    post_snapshot_id: str
    comparison: SnapshotComparison
    rollback_recommended: bool
    rollback_strategy: str


class ChangePlan(BaseModel):
    """Immutable requested change plus approval and verification lifecycle."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = CHANGE_PLAN_SCHEMA_VERSION
    id: str
    tenant_id: str
    created_by: str
    created_at: datetime
    expires_at: datetime
    status: PlanStatus = "planned"
    operation: ChangeOperation
    intent: str
    target_id: str
    targets: tuple[str, ...]
    canary_targets: tuple[str, ...]
    pre_snapshot_id: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    expected_outcomes: tuple[str, ...] = ()
    rollback_strategy: str
    plan_hash: str
    approval: ChangeApproval | None = None
    verification: ChangeVerification | None = None
    execution_enabled: Literal[False] = False


class ChangePlanStore:
    """Persist guarded change plans without providing any execution operation."""

    def __init__(self, state_dir: Path, *, max_plans: int = DEFAULT_MAX_PLANS) -> None:
        if not 1 <= max_plans <= 10_000:
            raise ValueError("max_plans must be between 1 and 10000")
        self.state_dir = Path(state_dir).expanduser()
        self.database_path = self.state_dir / "change-plans.sqlite3"
        self.key_path = self.state_dir / "change-plans.key"
        self.max_plans = max_plans
        self._lock = threading.RLock()

        prepare_private_directory(self.state_dir)
        self._cipher = load_or_create_fernet(
            self.key_path,
            error_type=ChangePlanStoreError,
            error_message="Change plan encryption key is invalid",
        )
        self._migrate()

    def create(
        self,
        *,
        actor: GovernanceActor,
        operation: ChangeOperation,
        intent: str,
        target_id: str,
        targets: tuple[str, ...],
        canary_targets: tuple[str, ...],
        pre_snapshot_id: str,
        parameters: dict[str, Any] | None = None,
        expected_outcomes: tuple[str, ...] = (),
        rollback_strategy: str = "Restore the previous approved state.",
        ttl_minutes: int = 60,
        now: datetime | None = None,
    ) -> ChangePlan:
        """Create an immutable plan for one bounded template or policy workflow."""
        self._authorize_human(actor, allow_operator=True)
        if not 5 <= ttl_minutes <= 10_080:
            raise ValueError("ttl_minutes must be between 5 and 10080")
        safe_intent = str(sanitize_investigation_data(intent)).strip()[:1_000]
        safe_target_id = str(sanitize_investigation_data(target_id)).strip()[:500]
        safe_targets = tuple(dict.fromkeys(str(item).strip()[:500] for item in targets if str(item).strip()))
        safe_canaries = tuple(
            dict.fromkeys(str(item).strip()[:500] for item in canary_targets if str(item).strip())
        )
        if not safe_intent or not safe_target_id or not safe_targets:
            raise ValueError("intent, target_id, and targets are required")
        if not set(safe_canaries).issubset(safe_targets):
            raise ValueError("canary_targets must be a subset of targets")
        if operation == "attach-device-template" and not safe_canaries:
            raise ValueError("attach-device-template plans require at least one canary")
        created_at = now or datetime.now(UTC)
        plan_id = str(uuid4())
        safe_parameters = cast(
            dict[str, Any],
            sanitize_investigation_data(parameters or {}),
        )
        safe_outcomes = tuple(
            str(sanitize_investigation_data(outcome)).strip()[:500]
            for outcome in expected_outcomes[:20]
            if str(outcome).strip()
        )
        safe_rollback = str(sanitize_investigation_data(rollback_strategy)).strip()[:2_000]
        immutable = {
            "schema_version": CHANGE_PLAN_SCHEMA_VERSION,
            "id": plan_id,
            "tenant_id": actor.tenant_id,
            "created_by": actor.id,
            "created_at": created_at.isoformat(),
            "expires_at": (created_at + timedelta(minutes=ttl_minutes)).isoformat(),
            "operation": operation,
            "intent": safe_intent,
            "target_id": safe_target_id,
            "targets": safe_targets,
            "canary_targets": safe_canaries,
            "pre_snapshot_id": pre_snapshot_id,
            "parameters": safe_parameters,
            "expected_outcomes": safe_outcomes,
            "rollback_strategy": safe_rollback,
        }
        plan = ChangePlan(
            **immutable,
            plan_hash=canonical_sha256(immutable),
        )
        with self._lock, private_sqlite_connection(self.database_path) as connection:
            self._write(connection, plan)
            self._prune(connection)
        return plan

    def get(self, plan_id: str, *, actor: GovernanceActor) -> ChangePlan | None:
        """Load a plan only within the actor's tenant."""
        self._authorize_human(actor, allow_operator=True)
        with self._lock, private_sqlite_connection(self.database_path) as connection:
            row = connection.execute(
                "SELECT payload FROM change_plans WHERE id = ?",
                (plan_id,),
            ).fetchone()
        if row is None:
            return None
        plan = self._decrypt(row[0])
        self._authorize_tenant(actor, plan)
        return plan

    def list(self, *, actor: GovernanceActor) -> list[ChangePlan]:
        """List plans visible to the actor's tenant."""
        self._authorize_human(actor, allow_operator=True)
        with self._lock, private_sqlite_connection(self.database_path) as connection:
            rows = connection.execute(
                "SELECT payload FROM change_plans ORDER BY created_at DESC, rowid DESC"
            ).fetchall()
        return [
            plan
            for row in rows
            if (plan := self._decrypt(row[0])).tenant_id == actor.tenant_id
        ]

    def approve(
        self,
        plan_id: str,
        *,
        actor: GovernanceActor,
        expected_hash: str,
        approval_minutes: int = 15,
        now: datetime | None = None,
    ) -> ChangePlan:
        """Approve an exact plan hash for a short bounded window."""
        self._authorize_human(actor, allow_operator=False)
        if not 1 <= approval_minutes <= 60:
            raise ValueError("approval_minutes must be between 1 and 60")
        plan = self._require(plan_id, actor)
        if expected_hash != plan.plan_hash:
            raise ValueError("Approval plan hash does not match the plan")
        approved_at = now or datetime.now(UTC)
        if approved_at >= plan.expires_at:
            self._save(plan.model_copy(update={"status": "expired"}))
            raise ValueError("Change plan has expired")
        if plan.status == "approved":
            return plan
        if plan.status != "planned":
            raise ValueError(f"Change plan cannot be approved from status {plan.status}")
        approved = plan.model_copy(
            update={
                "status": "approved",
                "approval": ChangeApproval(
                    actor_id=actor.id,
                    approved_at=approved_at,
                    expires_at=approved_at + timedelta(minutes=approval_minutes),
                    plan_hash=plan.plan_hash,
                ),
            }
        )
        self._save(approved)
        return approved

    def verify(
        self,
        plan_id: str,
        *,
        actor: GovernanceActor,
        before: FabricSnapshot,
        after: FabricSnapshot,
        now: datetime | None = None,
    ) -> ChangePlan:
        """Compare post-change evidence and produce rollback decision support."""
        self._authorize_human(actor, allow_operator=True)
        plan = self._require(plan_id, actor)
        verified_at = now or datetime.now(UTC)
        if plan.status != "approved" or plan.approval is None:
            raise ValueError("Change plan must be approved before verification")
        if verified_at >= plan.approval.expires_at:
            self._save(plan.model_copy(update={"status": "expired"}))
            raise ValueError("Change approval has expired")
        if before.id != plan.pre_snapshot_id:
            raise ValueError("Before snapshot does not match the change plan")
        comparison = compare_snapshots(before, after)
        rollback_recommended = comparison.verdict in {"regressed", "mixed"}
        status: PlanStatus = (
            "verification-failed" if rollback_recommended else "verification-passed"
        )
        verification = ChangeVerification(
            actor_id=actor.id,
            verified_at=verified_at,
            post_snapshot_id=after.id,
            comparison=comparison,
            rollback_recommended=rollback_recommended,
            rollback_strategy=plan.rollback_strategy,
        )
        verified = plan.model_copy(
            update={"status": status, "verification": verification}
        )
        self._save(verified)
        return verified

    def cancel(
        self,
        plan_id: str,
        *,
        actor: GovernanceActor,
    ) -> ChangePlan:
        """Cancel a plan without performing any network operation."""
        self._authorize_human(actor, allow_operator=False)
        plan = self._require(plan_id, actor)
        if plan.status in {"verification-passed", "verification-failed"}:
            raise ValueError("Verified change plans cannot be cancelled")
        cancelled = plan.model_copy(update={"status": "cancelled"})
        self._save(cancelled)
        return cancelled

    def assert_execution_enabled(self, plan_id: str) -> None:
        """Fail closed because this package contains no write-capable executor."""
        raise ExecutionDisabledError(
            f"Execution is disabled for change plan {plan_id}; this build is planning-only"
        )

    def _require(self, plan_id: str, actor: GovernanceActor) -> ChangePlan:
        plan = self.get(plan_id, actor=actor)
        if plan is None:
            raise KeyError(f"Change plan not found: {plan_id}")
        return plan

    @staticmethod
    def _authorize_human(actor: GovernanceActor, *, allow_operator: bool) -> None:
        allowed = {"owner", "operator"} if allow_operator else {"owner"}
        if not actor.active or actor.role not in allowed:
            raise AuthorizationError("Actor is not authorized")

    @staticmethod
    def _authorize_tenant(actor: GovernanceActor, plan: ChangePlan) -> None:
        if actor.tenant_id != plan.tenant_id:
            raise AuthorizationError("Actor is not authorized")

    def _save(self, plan: ChangePlan) -> None:
        with self._lock, private_sqlite_connection(self.database_path) as connection:
            self._write(connection, plan)

    def _write(self, connection: sqlite3.Connection, plan: ChangePlan) -> None:
        payload = self._cipher.encrypt(canonical_json_bytes(plan.model_dump(mode="json")))
        connection.execute(
            """
            INSERT INTO change_plans (id, tenant_id, created_at, payload)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET payload = excluded.payload
            """,
            (plan.id, plan.tenant_id, plan.created_at.isoformat(), payload),
        )

    def _prune(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            DELETE FROM change_plans
            WHERE id IN (
                SELECT id FROM change_plans
                ORDER BY created_at DESC, rowid DESC
                LIMIT -1 OFFSET ?
            )
            """,
            (self.max_plans,),
        )

    def _decrypt(self, payload: bytes) -> ChangePlan:
        try:
            return ChangePlan.model_validate_json(self._cipher.decrypt(payload))
        except (InvalidToken, ValueError) as exc:
            raise ChangePlanStoreError("Change plan payload is unreadable") from exc

    def _migrate(self) -> None:
        with self._lock, private_sqlite_connection(self.database_path) as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version > CHANGE_PLAN_SCHEMA_VERSION:
                raise ChangePlanStoreError(
                    f"Change plan schema {version} is newer than supported schema "
                    f"{CHANGE_PLAN_SCHEMA_VERSION}"
                )
            if version == 0:
                connection.execute(
                    """
                    CREATE TABLE change_plans (
                        id TEXT PRIMARY KEY,
                        tenant_id TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        payload BLOB NOT NULL
                    )
                    """
                )
                connection.execute(
                    "CREATE INDEX change_plan_created_at ON change_plans(created_at DESC)"
                )
                connection.execute(f"PRAGMA user_version = {CHANGE_PLAN_SCHEMA_VERSION}")
