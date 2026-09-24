"""Local collaboration records, tenant-aware RBAC, and agent policy."""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast
from uuid import uuid4

from cryptography.fernet import InvalidToken
from pydantic import BaseModel, ConfigDict, Field, model_validator

from cisco_vmanage_mcp.services.connectors import ConnectorDescriptor, DataClassification
from cisco_vmanage_mcp.services.investigations import (
    Investigation,
    sanitize_investigation_data,
)
from cisco_vmanage_mcp.services.persistence import (
    canonical_json_bytes,
    load_or_create_fernet,
    prepare_private_directory,
    private_sqlite_connection,
)

GOVERNANCE_SCHEMA_VERSION = 1
DEFAULT_MAX_COLLABORATIONS = 500
DEFAULT_MAX_EVENTS = 500

ActorRole = Literal["owner", "operator", "viewer", "agent"]
CollaborationStatus = Literal["open", "monitoring", "resolved"]
EventKind = Literal[
    "created",
    "comment",
    "evidence",
    "hypothesis",
    "ownership",
    "status",
]
RoleAudience = Literal["executive", "noc", "netops", "secops"]
HypothesisConfidence = Literal["low", "medium", "high"]

_CLASSIFICATION_WEIGHT: dict[DataClassification, int] = {
    "operational": 0,
    "restricted": 1,
    "sensitive": 2,
}


class AuthorizationError(PermissionError):
    """Raised when an actor cannot access or mutate a governed resource."""


class ConflictError(RuntimeError):
    """Raised when a mutation uses an outdated collaboration revision."""


class GovernanceStoreError(RuntimeError):
    """Raised when encrypted governance state cannot be initialized or read."""


class GovernanceActor(BaseModel):
    """A human or agent identity scoped to one tenant."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=200)
    tenant_id: str = Field(min_length=1, max_length=200)
    display_name: str = Field(min_length=1, max_length=200)
    role: ActorRole
    human_owner_id: str | None = None
    purpose: str | None = Field(default=None, max_length=500)
    allowed_connectors: tuple[str, ...] = ()
    allowed_actions: tuple[str, ...] = ()
    maximum_data_classification: DataClassification = "operational"
    active: bool = True

    @model_validator(mode="after")
    def validate_agent_owner(self) -> GovernanceActor:
        if self.role == "agent" and not self.human_owner_id:
            raise ValueError("Agent identities require a human_owner_id")
        if self.role != "agent" and self.human_owner_id is not None:
            raise ValueError("Only agent identities can declare a human_owner_id")
        return self


def default_read_only_agents(
    human_owner_id: str,
    tenant_id: str,
) -> tuple[GovernanceActor, ...]:
    """Return the built-in read-only specialist agent identities."""
    profiles = (
        (
            "topology-agent",
            "Topology Agent",
            "Normalizes sites, devices, TLOCs, tunnels, and control relationships.",
            ("vmanage",),
            ("topology", "inventory"),
            "restricted",
        ),
        (
            "assurance-agent",
            "Assurance Agent",
            "Compares snapshots and correlates path and application assurance evidence.",
            ("vmanage", "thousandeyes", "appdynamics"),
            ("assurance", "path-evidence", "application-evidence"),
            "restricted",
        ),
        (
            "security-agent",
            "Security Agent",
            "Reviews security-relevant alarms and configured event evidence.",
            ("vmanage", "splunk"),
            ("alarms", "events"),
            "sensitive",
        ),
        (
            "change-readiness-agent",
            "Change Readiness Agent",
            "Evaluates read-only pre-change evidence and blockers.",
            ("vmanage",),
            ("change-readiness",),
            "restricted",
        ),
    )
    return tuple(
        GovernanceActor(
            id=actor_id,
            tenant_id=tenant_id,
            display_name=display_name,
            role="agent",
            human_owner_id=human_owner_id,
            purpose=purpose,
            allowed_connectors=connectors,
            allowed_actions=actions,
            maximum_data_classification=cast(DataClassification, classification),
        )
        for actor_id, display_name, purpose, connectors, actions, classification in profiles
    )


class CollaborationEvent(BaseModel):
    """One immutable event in a collaboration timeline."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: str(uuid4()))
    kind: EventKind
    actor_id: str
    created_at: datetime
    body: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    observed_at: datetime | None = None
    stale_after_seconds: int | None = None
    stale: bool = False


class CollaborationHypothesis(BaseModel):
    """A user-authored hypothesis whose confidence can evolve explicitly."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: str(uuid4()))
    title: str
    confidence: HypothesisConfidence
    created_by: str
    created_at: datetime
    updated_at: datetime


class CollaborationRecord(BaseModel):
    """Versioned ownership and timeline state for one investigation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = GOVERNANCE_SCHEMA_VERSION
    investigation_id: str
    tenant_id: str
    owner_id: str
    status: CollaborationStatus = "open"
    revision: int = 1
    created_at: datetime
    updated_at: datetime
    events: tuple[CollaborationEvent, ...]
    hypotheses: tuple[CollaborationHypothesis, ...] = ()


class RoleAwareInvestigationView(BaseModel):
    """A deterministic investigation projection for one operational audience."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = GOVERNANCE_SCHEMA_VERSION
    audience: RoleAudience
    investigation_id: str
    title: str
    status: CollaborationStatus
    owner_id: str
    summary: str
    message_count: int
    pin_count: int
    messages: tuple[dict[str, Any], ...]
    evidence: tuple[dict[str, Any], ...]
    timeline: tuple[CollaborationEvent, ...]


def build_role_view(
    investigation: Investigation,
    collaboration: CollaborationRecord,
    audience: RoleAudience,
) -> RoleAwareInvestigationView:
    """Project one investigation into a bounded audience-specific data shape."""
    if investigation.id != collaboration.investigation_id:
        raise ValueError("Investigation and collaboration IDs do not match")
    assistant_messages = [
        message for message in investigation.messages if message.role == "assistant"
    ]
    summary = assistant_messages[-1].content if assistant_messages else investigation.title
    if audience == "executive":
        messages: tuple[dict[str, Any], ...] = ()
        evidence = tuple(
            {
                "type": pin.get("type", "source"),
                "label": pin.get("label", "Evidence"),
            }
            for pin in investigation.pins[:10]
        )
        timeline = tuple(
            event
            for event in collaboration.events
            if event.kind in {"created", "ownership", "status"}
        )[-10:]
    else:
        message_limit = 20 if audience == "netops" else 10
        messages = tuple(
            {
                "role": message.role,
                "content": message.content,
                "created_at": message.created_at.isoformat(),
            }
            for message in investigation.messages[-message_limit:]
        )
        selected_pins = (
            [pin for pin in investigation.pins if pin.get("type") in {"alarm", "source"}]
            if audience == "secops"
            else investigation.pins
        )
        if audience == "noc":
            allowed_data = {"site_id", "severity", "state", "health", "reachability"}
            evidence = tuple(
                {
                    "type": pin.get("type", "source"),
                    "label": pin.get("label", "Evidence"),
                    "data": {
                        key: value
                        for key, value in pin.get("data", {}).items()
                        if key in allowed_data
                    },
                }
                for pin in selected_pins[:20]
            )
        else:
            evidence = tuple(
                cast(dict[str, Any], sanitize_investigation_data(pin))
                for pin in selected_pins[:50]
            )
        timeline = collaboration.events[-50:]
    return RoleAwareInvestigationView(
        audience=audience,
        investigation_id=investigation.id,
        title=investigation.title,
        status=collaboration.status,
        owner_id=collaboration.owner_id,
        summary=summary,
        message_count=len(investigation.messages),
        pin_count=len(investigation.pins),
        messages=messages,
        evidence=evidence,
        timeline=timeline,
    )


class GovernanceStore:
    """Persist encrypted actors and collaboration records with policy checks."""

    def __init__(
        self,
        state_dir: Path,
        *,
        bootstrap_owner: GovernanceActor | None = None,
        max_collaborations: int = DEFAULT_MAX_COLLABORATIONS,
        max_events: int = DEFAULT_MAX_EVENTS,
    ) -> None:
        if not 1 <= max_collaborations <= 10_000:
            raise ValueError("max_collaborations must be between 1 and 10000")
        if not 1 <= max_events <= 10_000:
            raise ValueError("max_events must be between 1 and 10000")
        self.state_dir = Path(state_dir).expanduser()
        self.database_path = self.state_dir / "governance.sqlite3"
        self.key_path = self.state_dir / "governance.key"
        self.max_collaborations = max_collaborations
        self.max_events = max_events
        self._lock = threading.RLock()

        prepare_private_directory(self.state_dir)
        self._cipher = load_or_create_fernet(
            self.key_path,
            error_type=GovernanceStoreError,
            error_message="Governance encryption key is invalid",
        )
        self._migrate()
        if bootstrap_owner is not None:
            if bootstrap_owner.role != "owner":
                raise ValueError("bootstrap_owner must have the owner role")
            with self._lock:
                try:
                    self.get_actor(bootstrap_owner.id)
                except KeyError:
                    self._save_actor(bootstrap_owner)

    def register_actor(
        self,
        requester_id: str,
        actor: GovernanceActor,
        *,
        allow_cross_tenant: bool = False,
    ) -> GovernanceActor:
        """Register an identity when an owner authorizes the operation."""
        requester = self.get_actor(requester_id)
        if requester.role != "owner" or not requester.active:
            raise AuthorizationError("Actor is not authorized")
        if requester.tenant_id != actor.tenant_id and not allow_cross_tenant:
            raise AuthorizationError("Actor is not authorized")
        if actor.role == "agent":
            human_owner = self.get_actor(actor.human_owner_id or "")
            if (
                human_owner.role == "agent"
                or human_owner.tenant_id != actor.tenant_id
                or not human_owner.active
            ):
                raise AuthorizationError("Actor is not authorized")
        with self._lock, private_sqlite_connection(self.database_path) as connection:
            if connection.execute(
                "SELECT 1 FROM governance_actors WHERE id = ?",
                (actor.id,),
            ).fetchone():
                raise ValueError(f"Actor already registered: {actor.id}")
            self._write_actor(connection, actor)
        return actor

    def synchronize_authenticated_actor(self, actor: GovernanceActor) -> GovernanceActor:
        """Upsert a human actor after authentication at the application boundary."""
        if actor.role == "agent" or actor.human_owner_id is not None:
            raise AuthorizationError("Authenticated identities must represent human actors")
        with self._lock, private_sqlite_connection(self.database_path) as connection:
            row = connection.execute(
                "SELECT payload FROM governance_actors WHERE id = ?",
                (actor.id,),
            ).fetchone()
            if row is not None:
                existing = self._decrypt_actor(row[0])
                if existing.tenant_id != actor.tenant_id:
                    raise AuthorizationError("Actor tenant cannot be changed")
            self._write_actor(connection, actor)
        return actor

    def get_actor(self, actor_id: str) -> GovernanceActor:
        """Load one registered identity."""
        with self._lock, private_sqlite_connection(self.database_path) as connection:
            row = connection.execute(
                "SELECT payload FROM governance_actors WHERE id = ?",
                (actor_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Actor not found: {actor_id}")
        return self._decrypt_actor(row[0])

    def list_actors(self, requester_id: str) -> list[GovernanceActor]:
        """List active identities in the requester's tenant."""
        requester = self.get_actor(requester_id)
        with self._lock, private_sqlite_connection(self.database_path) as connection:
            rows = connection.execute("SELECT payload FROM governance_actors").fetchall()
        return sorted(
            (
                actor
                for row in rows
                if (actor := self._decrypt_actor(row[0])).tenant_id == requester.tenant_id
            ),
            key=lambda actor: actor.id,
        )

    def create_collaboration(
        self,
        investigation_id: str,
        actor_id: str,
        *,
        now: datetime | None = None,
    ) -> CollaborationRecord:
        """Create an owner-scoped collaboration record idempotently."""
        actor = self._active_actor(actor_id)
        if actor.role == "viewer":
            raise AuthorizationError("Actor is not authorized")
        with self._lock:
            existing = self._get_record(investigation_id)
            if existing is not None:
                self._authorize_tenant(actor, existing.tenant_id)
                return self._view(existing, now or datetime.now(UTC))
            created_at = now or datetime.now(UTC)
            record = CollaborationRecord(
                investigation_id=investigation_id,
                tenant_id=actor.tenant_id,
                owner_id=actor.id,
                created_at=created_at,
                updated_at=created_at,
                events=(
                    CollaborationEvent(
                        kind="created",
                        actor_id=actor.id,
                        created_at=created_at,
                        body="Investigation collaboration created",
                    ),
                ),
            )
            self._save_record(record)
            self._prune_collaborations()
            return self._view(record, created_at)

    def get_collaboration(
        self,
        investigation_id: str,
        actor_id: str,
        *,
        now: datetime | None = None,
    ) -> CollaborationRecord:
        """Load a collaboration only within the actor's tenant."""
        actor = self._active_actor(actor_id)
        record = self._require_record(investigation_id)
        self._authorize_tenant(actor, record.tenant_id)
        return self._view(record, now or datetime.now(UTC))

    def list_collaborations(self, actor_id: str) -> list[CollaborationRecord]:
        """List collaboration records visible to one tenant-scoped actor."""
        actor = self._active_actor(actor_id)
        with self._lock, private_sqlite_connection(self.database_path) as connection:
            rows = connection.execute(
                "SELECT payload FROM collaborations ORDER BY updated_at DESC, rowid DESC"
            ).fetchall()
        return [
            self._view(record, datetime.now(UTC))
            for row in rows
            if (record := self._decrypt_record(row[0])).tenant_id == actor.tenant_id
        ]

    def add_comment(
        self,
        investigation_id: str,
        actor_id: str,
        body: str,
        *,
        expected_revision: int,
        now: datetime | None = None,
    ) -> CollaborationRecord:
        """Append a sanitized comment with optimistic concurrency."""
        return self._append_event(
            investigation_id,
            actor_id,
            kind="comment",
            body=body,
            expected_revision=expected_revision,
            now=now,
        )

    def add_evidence_reference(
        self,
        investigation_id: str,
        actor_id: str,
        body: str,
        *,
        observed_at: datetime,
        stale_after_seconds: int,
        expected_revision: int,
        now: datetime | None = None,
    ) -> CollaborationRecord:
        """Append a source reference with an explicit staleness threshold."""
        if not 1 <= stale_after_seconds <= 31_536_000:
            raise ValueError("stale_after_seconds must be between 1 and 31536000")
        return self._append_event(
            investigation_id,
            actor_id,
            kind="evidence",
            body=body,
            observed_at=observed_at,
            stale_after_seconds=stale_after_seconds,
            expected_revision=expected_revision,
            now=now,
        )

    def add_hypothesis(
        self,
        investigation_id: str,
        actor_id: str,
        title: str,
        *,
        confidence: HypothesisConfidence,
        expected_revision: int,
        now: datetime | None = None,
    ) -> CollaborationRecord:
        """Add a competing hypothesis to a revisioned investigation."""
        actor = self._active_actor(actor_id)
        if actor.role == "viewer":
            raise AuthorizationError("Actor is not authorized")
        safe_title = str(sanitize_investigation_data(title)).strip()[:1_000]
        if not safe_title:
            raise ValueError("Hypothesis title is required")
        with self._lock:
            record = self._require_record(investigation_id)
            self._authorize_tenant(actor, record.tenant_id)
            self._check_revision(record, expected_revision)
            changed_at = now or datetime.now(UTC)
            hypothesis = CollaborationHypothesis(
                title=safe_title,
                confidence=confidence,
                created_by=actor.id,
                created_at=changed_at,
                updated_at=changed_at,
            )
            event = CollaborationEvent(
                kind="hypothesis",
                actor_id=actor.id,
                created_at=changed_at,
                body=f"Added {confidence}-confidence hypothesis: {safe_title}",
                metadata={"hypothesis_id": hypothesis.id, "confidence": confidence},
            )
            updated = record.model_copy(
                update={
                    "revision": record.revision + 1,
                    "updated_at": changed_at,
                    "hypotheses": (*record.hypotheses, hypothesis),
                    "events": (*record.events, event)[-self.max_events :],
                }
            )
            self._save_record(updated)
            return self._view(updated, changed_at)

    def update_hypothesis(
        self,
        investigation_id: str,
        actor_id: str,
        hypothesis_id: str,
        *,
        confidence: HypothesisConfidence,
        expected_revision: int,
        now: datetime | None = None,
    ) -> CollaborationRecord:
        """Change confidence while retaining the prior timeline history."""
        actor = self._active_actor(actor_id)
        if actor.role == "viewer":
            raise AuthorizationError("Actor is not authorized")
        with self._lock:
            record = self._require_record(investigation_id)
            self._authorize_tenant(actor, record.tenant_id)
            self._check_revision(record, expected_revision)
            existing = next(
                (item for item in record.hypotheses if item.id == hypothesis_id),
                None,
            )
            if existing is None:
                raise KeyError(f"Hypothesis not found: {hypothesis_id}")
            changed_at = now or datetime.now(UTC)
            changed = existing.model_copy(
                update={"confidence": confidence, "updated_at": changed_at}
            )
            hypotheses = tuple(
                changed if item.id == hypothesis_id else item
                for item in record.hypotheses
            )
            event = CollaborationEvent(
                kind="hypothesis",
                actor_id=actor.id,
                created_at=changed_at,
                body=f"Changed hypothesis confidence to {confidence}: {existing.title}",
                metadata={"hypothesis_id": existing.id, "confidence": confidence},
            )
            updated = record.model_copy(
                update={
                    "revision": record.revision + 1,
                    "updated_at": changed_at,
                    "hypotheses": hypotheses,
                    "events": (*record.events, event)[-self.max_events :],
                }
            )
            self._save_record(updated)
            return self._view(updated, changed_at)

    def assign_owner(
        self,
        investigation_id: str,
        actor_id: str,
        assignee_id: str,
        *,
        expected_revision: int,
        now: datetime | None = None,
    ) -> CollaborationRecord:
        """Assign a collaboration to a same-tenant human actor."""
        actor = self._active_actor(actor_id)
        if actor.role != "owner":
            raise AuthorizationError("Actor is not authorized")
        assignee = self._active_actor(assignee_id)
        if assignee.role == "agent" or assignee.tenant_id != actor.tenant_id:
            raise AuthorizationError("Actor is not authorized")
        with self._lock:
            record = self._require_record(investigation_id)
            self._authorize_tenant(actor, record.tenant_id)
            self._check_revision(record, expected_revision)
            changed_at = now or datetime.now(UTC)
            event = CollaborationEvent(
                kind="ownership",
                actor_id=actor.id,
                created_at=changed_at,
                body=f"Assigned to {assignee.display_name}",
                metadata={"owner_id": assignee.id},
            )
            updated = record.model_copy(
                update={
                    "owner_id": assignee.id,
                    "revision": record.revision + 1,
                    "updated_at": changed_at,
                    "events": (*record.events, event)[-self.max_events :],
                }
            )
            self._save_record(updated)
            return self._view(updated, changed_at)

    def authorize_connector(
        self,
        actor_id: str,
        connector: ConnectorDescriptor,
    ) -> bool:
        """Return whether an agent may access a connector and its data class."""
        try:
            actor = self._active_actor(actor_id)
        except (AuthorizationError, KeyError):
            return False
        if actor.role != "agent" or not connector.enabled:
            return False
        if connector.id not in actor.allowed_connectors:
            return False
        return (
            _CLASSIFICATION_WEIGHT[connector.data_classification]
            <= _CLASSIFICATION_WEIGHT[actor.maximum_data_classification]
        )

    def _append_event(
        self,
        investigation_id: str,
        actor_id: str,
        *,
        kind: Literal["comment", "evidence"],
        body: str,
        expected_revision: int,
        now: datetime | None,
        observed_at: datetime | None = None,
        stale_after_seconds: int | None = None,
    ) -> CollaborationRecord:
        actor = self._active_actor(actor_id)
        if actor.role == "viewer":
            raise AuthorizationError("Actor is not authorized")
        safe_body = str(sanitize_investigation_data(body)).strip()[:4_000]
        if not safe_body:
            raise ValueError("Timeline event body is required")
        with self._lock:
            record = self._require_record(investigation_id)
            self._authorize_tenant(actor, record.tenant_id)
            self._check_revision(record, expected_revision)
            created_at = now or datetime.now(UTC)
            event = CollaborationEvent(
                kind=kind,
                actor_id=actor.id,
                created_at=created_at,
                body=safe_body,
                observed_at=observed_at,
                stale_after_seconds=stale_after_seconds,
            )
            updated = record.model_copy(
                update={
                    "revision": record.revision + 1,
                    "updated_at": created_at,
                    "events": (*record.events, event)[-self.max_events :],
                }
            )
            self._save_record(updated)
            return self._view(updated, created_at)

    def _active_actor(self, actor_id: str) -> GovernanceActor:
        actor = self.get_actor(actor_id)
        if not actor.active:
            raise AuthorizationError("Actor is not authorized")
        return actor

    @staticmethod
    def _authorize_tenant(actor: GovernanceActor, tenant_id: str) -> None:
        if actor.tenant_id != tenant_id:
            raise AuthorizationError("Actor is not authorized")

    @staticmethod
    def _check_revision(record: CollaborationRecord, expected_revision: int) -> None:
        if record.revision != expected_revision:
            raise ConflictError(
                f"Collaboration revision conflict: expected {expected_revision}, "
                f"current {record.revision}"
            )

    def _view(self, record: CollaborationRecord, now: datetime) -> CollaborationRecord:
        events = tuple(
            event.model_copy(
                update={
                    "stale": bool(
                        event.observed_at is not None
                        and event.stale_after_seconds is not None
                        and now
                        > event.observed_at + timedelta(seconds=event.stale_after_seconds)
                    )
                }
            )
            for event in record.events
        )
        return record.model_copy(update={"events": events})

    def _get_record(self, investigation_id: str) -> CollaborationRecord | None:
        with private_sqlite_connection(self.database_path) as connection:
            row = connection.execute(
                "SELECT payload FROM collaborations WHERE investigation_id = ?",
                (investigation_id,),
            ).fetchone()
        return self._decrypt_record(row[0]) if row else None

    def _require_record(self, investigation_id: str) -> CollaborationRecord:
        record = self._get_record(investigation_id)
        if record is None:
            raise KeyError(f"Collaboration not found: {investigation_id}")
        return record

    def _save_actor(self, actor: GovernanceActor) -> None:
        with self._lock, private_sqlite_connection(self.database_path) as connection:
            self._write_actor(connection, actor)

    def _write_actor(self, connection: sqlite3.Connection, actor: GovernanceActor) -> None:
        payload = self._cipher.encrypt(canonical_json_bytes(actor.model_dump(mode="json")))
        connection.execute(
            "INSERT OR REPLACE INTO governance_actors (id, payload) VALUES (?, ?)",
            (actor.id, payload),
        )

    def _save_record(self, record: CollaborationRecord) -> None:
        payload = self._cipher.encrypt(canonical_json_bytes(record.model_dump(mode="json")))
        with private_sqlite_connection(self.database_path) as connection:
            connection.execute(
                """
                INSERT INTO collaborations (investigation_id, updated_at, payload)
                VALUES (?, ?, ?)
                ON CONFLICT(investigation_id) DO UPDATE SET
                    updated_at = excluded.updated_at,
                    payload = excluded.payload
                """,
                (record.investigation_id, record.updated_at.isoformat(), payload),
            )

    def _prune_collaborations(self) -> None:
        with private_sqlite_connection(self.database_path) as connection:
            connection.execute(
                """
                DELETE FROM collaborations
                WHERE investigation_id IN (
                    SELECT investigation_id FROM collaborations
                    ORDER BY updated_at DESC, rowid DESC
                    LIMIT -1 OFFSET ?
                )
                """,
                (self.max_collaborations,),
            )

    def _decrypt_actor(self, payload: bytes) -> GovernanceActor:
        try:
            return GovernanceActor.model_validate_json(self._cipher.decrypt(payload))
        except (InvalidToken, ValueError) as exc:
            raise GovernanceStoreError("Actor payload is unreadable") from exc

    def _decrypt_record(self, payload: bytes) -> CollaborationRecord:
        try:
            return CollaborationRecord.model_validate_json(self._cipher.decrypt(payload))
        except (InvalidToken, ValueError) as exc:
            raise GovernanceStoreError("Collaboration payload is unreadable") from exc

    def _migrate(self) -> None:
        with self._lock, private_sqlite_connection(self.database_path) as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version > GOVERNANCE_SCHEMA_VERSION:
                raise GovernanceStoreError(
                    f"Governance schema {version} is newer than supported schema "
                    f"{GOVERNANCE_SCHEMA_VERSION}"
                )
            if version == 0:
                connection.execute(
                    "CREATE TABLE governance_actors (id TEXT PRIMARY KEY, payload BLOB NOT NULL)"
                )
                connection.execute(
                    """
                    CREATE TABLE collaborations (
                        investigation_id TEXT PRIMARY KEY,
                        updated_at TEXT NOT NULL,
                        payload BLOB NOT NULL
                    )
                    """
                )
                connection.execute(
                    "CREATE INDEX collaboration_updated_at "
                    "ON collaborations(updated_at DESC)"
                )
                connection.execute(f"PRAGMA user_version = {GOVERNANCE_SCHEMA_VERSION}")
