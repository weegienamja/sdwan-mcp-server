"""Preview-only external workflow drafts with explicit local approval."""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast
from uuid import uuid4

from cryptography.fernet import InvalidToken
from pydantic import BaseModel, ConfigDict, Field

from cisco_vmanage_mcp.services.investigations import (
    Investigation,
    sanitize_investigation_data,
)
from cisco_vmanage_mcp.services.persistence import (
    canonical_json_bytes,
    canonical_sha256,
    load_or_create_fernet,
    prepare_private_directory,
    private_sqlite_connection,
)

WORKFLOW_SCHEMA_VERSION = 1
DEFAULT_MAX_DRAFTS = 500

WorkflowDestination = Literal["servicenow", "webex", "slack"]
WorkflowStatus = Literal["draft", "approved", "cancelled", "expired"]
WorkflowAction = Literal["drafted", "approved", "cancelled", "expired"]


class WorkflowApproval(BaseModel):
    """An approval bound to the exact immutable preview hash."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    actor_id: str
    approved_at: datetime
    content_hash: str


class WorkflowAuditEvent(BaseModel):
    """One local workflow lifecycle record."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    action: WorkflowAction
    actor_id: str
    occurred_at: datetime
    content_hash: str


class WorkflowDraft(BaseModel):
    """Immutable outbound preview plus mutable local lifecycle metadata."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = WORKFLOW_SCHEMA_VERSION
    id: str = Field(default_factory=lambda: str(uuid4()))
    investigation_id: str
    destination: WorkflowDestination
    target: str | None = None
    created_at: datetime
    expires_at: datetime
    status: WorkflowStatus = "draft"
    content_hash: str
    idempotency_key: str
    preview: dict[str, Any]
    approvals: tuple[WorkflowApproval, ...] = ()
    audit_events: tuple[WorkflowAuditEvent, ...]
    delivery_enabled: Literal[False] = False


class WorkflowStoreError(RuntimeError):
    """Raised when workflow draft persistence is unavailable or corrupt."""


def _summary(investigation: Investigation) -> str:
    messages = [
        message.content
        for message in investigation.messages[-10:]
        if message.role in {"assistant", "system"}
    ]
    pinned = [str(pin.get("label", "Evidence")) for pin in investigation.pins[-20:]]
    sections = [f"Investigation: {investigation.title}"]
    if messages:
        sections.extend(("", "Findings:", *[f"- {message}" for message in messages]))
    if pinned:
        sections.extend(("", "Pinned evidence:", *[f"- {label}" for label in pinned]))
    return "\n".join(sections)


def _build_preview(
    investigation: Investigation,
    destination: WorkflowDestination,
    target: str | None,
) -> dict[str, Any]:
    summary = _summary(investigation)
    pinned_data = [
        {
            "type": pin.get("type", "source"),
            "label": pin.get("label", "Evidence"),
            "data": pin.get("data", {}),
        }
        for pin in investigation.pins[-20:]
    ]
    if destination == "servicenow":
        preview = {
            "short_description": investigation.title[:160],
            "description": summary,
            "category": "network",
            "subcategory": "sd-wan",
            "assignment_group": target,
            "evidence": pinned_data,
        }
    elif destination == "webex":
        preview = {
            "room": target,
            "markdown": summary,
            "evidence": pinned_data,
        }
    else:
        preview = {
            "channel": target,
            "text": summary,
            "evidence": pinned_data,
        }
    return cast(dict[str, Any], sanitize_investigation_data(preview))


class WorkflowStore:
    """Persist encrypted workflow previews and approval audit records."""

    def __init__(self, state_dir: Path, *, max_drafts: int = DEFAULT_MAX_DRAFTS) -> None:
        if not 1 <= max_drafts <= 10_000:
            raise ValueError("max_drafts must be between 1 and 10000")
        self.state_dir = Path(state_dir).expanduser()
        self.database_path = self.state_dir / "workflows.sqlite3"
        self.key_path = self.state_dir / "workflows.key"
        self.max_drafts = max_drafts
        self._lock = threading.RLock()

        prepare_private_directory(self.state_dir)
        self._cipher = load_or_create_fernet(
            self.key_path,
            error_type=WorkflowStoreError,
            error_message="Workflow encryption key is invalid",
        )
        self._migrate()

    def create_draft(
        self,
        investigation: Investigation,
        destination: WorkflowDestination,
        *,
        target: str | None = None,
        actor_id: str = "local-operator",
        ttl_minutes: int = 60,
        now: datetime | None = None,
    ) -> WorkflowDraft:
        """Create or return an identical pending draft without sending it."""
        if not 5 <= ttl_minutes <= 10_080:
            raise ValueError("ttl_minutes must be between 5 and 10080")
        created_at = now or datetime.now(UTC)
        safe_target = (
            str(sanitize_investigation_data(target)).strip()[:200]
            if target is not None
            else None
        )
        safe_actor = str(sanitize_investigation_data(actor_id)).strip()[:200]
        preview = _build_preview(investigation, destination, safe_target)
        content_hash = canonical_sha256(preview)
        idempotency_key = canonical_sha256(
            {
                "destination": destination,
                "investigation_id": investigation.id,
                "target": safe_target,
                "content_hash": content_hash,
            }
        )
        with self._lock, private_sqlite_connection(self.database_path) as connection:
            existing = connection.execute(
                "SELECT payload FROM workflow_drafts WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                return self._decrypt(existing[0])
            draft = WorkflowDraft(
                investigation_id=investigation.id,
                destination=destination,
                target=safe_target,
                created_at=created_at,
                expires_at=created_at + timedelta(minutes=ttl_minutes),
                content_hash=content_hash,
                idempotency_key=idempotency_key,
                preview=preview,
                audit_events=(
                    WorkflowAuditEvent(
                        action="drafted",
                        actor_id=safe_actor,
                        occurred_at=created_at,
                        content_hash=content_hash,
                    ),
                ),
            )
            self._write(connection, draft)
            self._prune(connection)
        return draft

    def get(self, draft_id: str) -> WorkflowDraft | None:
        """Load one workflow draft."""
        with self._lock, private_sqlite_connection(self.database_path) as connection:
            row = connection.execute(
                "SELECT payload FROM workflow_drafts WHERE id = ?",
                (draft_id,),
            ).fetchone()
        return self._decrypt(row[0]) if row else None

    def list(self) -> list[WorkflowDraft]:
        """Return retained drafts newest first."""
        with self._lock, private_sqlite_connection(self.database_path) as connection:
            rows = connection.execute(
                "SELECT payload FROM workflow_drafts ORDER BY created_at DESC, rowid DESC"
            ).fetchall()
        return [self._decrypt(row[0]) for row in rows]

    def approve(
        self,
        draft_id: str,
        *,
        actor_id: str,
        expected_hash: str,
        now: datetime | None = None,
    ) -> WorkflowDraft:
        """Approve the exact preview hash locally without delivering it."""
        draft = self._require(draft_id)
        if expected_hash != draft.content_hash:
            raise ValueError("Approval content hash does not match the draft")
        if draft.status == "approved":
            return draft
        if draft.status == "cancelled":
            raise ValueError("Workflow draft is cancelled")
        approved_at = now or datetime.now(UTC)
        safe_actor = str(sanitize_investigation_data(actor_id)).strip()[:200]
        if approved_at >= draft.expires_at or draft.status == "expired":
            expired = draft.model_copy(
                update={
                    "status": "expired",
                    "audit_events": (*draft.audit_events, WorkflowAuditEvent(
                        action="expired",
                        actor_id=safe_actor,
                        occurred_at=approved_at,
                        content_hash=draft.content_hash,
                    )),
                }
            )
            self._save(expired)
            raise ValueError("Workflow draft has expired")
        approval = WorkflowApproval(
            actor_id=safe_actor,
            approved_at=approved_at,
            content_hash=draft.content_hash,
        )
        approved = draft.model_copy(
            update={
                "status": "approved",
                "approvals": (*draft.approvals, approval),
                "audit_events": (*draft.audit_events, WorkflowAuditEvent(
                    action="approved",
                    actor_id=safe_actor,
                    occurred_at=approved_at,
                    content_hash=draft.content_hash,
                )),
            }
        )
        self._save(approved)
        return approved

    def cancel(
        self,
        draft_id: str,
        *,
        actor_id: str,
        now: datetime | None = None,
    ) -> WorkflowDraft:
        """Cancel a pending local draft."""
        draft = self._require(draft_id)
        if draft.status == "cancelled":
            return draft
        if draft.status == "approved":
            raise ValueError("Approved workflow drafts cannot be cancelled")
        cancelled_at = now or datetime.now(UTC)
        safe_actor = str(sanitize_investigation_data(actor_id)).strip()[:200]
        cancelled = draft.model_copy(
            update={
                "status": "cancelled",
                "audit_events": (*draft.audit_events, WorkflowAuditEvent(
                    action="cancelled",
                    actor_id=safe_actor,
                    occurred_at=cancelled_at,
                    content_hash=draft.content_hash,
                )),
            }
        )
        self._save(cancelled)
        return cancelled

    def _require(self, draft_id: str) -> WorkflowDraft:
        draft = self.get(draft_id)
        if draft is None:
            raise KeyError(f"Workflow draft not found: {draft_id}")
        return draft

    def _save(self, draft: WorkflowDraft) -> None:
        with self._lock, private_sqlite_connection(self.database_path) as connection:
            self._write(connection, draft)

    def _write(self, connection: sqlite3.Connection, draft: WorkflowDraft) -> None:
        payload = self._cipher.encrypt(canonical_json_bytes(draft.model_dump(mode="json")))
        connection.execute(
            """
            INSERT INTO workflow_drafts (id, investigation_id, destination, created_at,
                                         idempotency_key, payload)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET payload = excluded.payload
            """,
            (
                draft.id,
                draft.investigation_id,
                draft.destination,
                draft.created_at.isoformat(),
                draft.idempotency_key,
                payload,
            ),
        )

    def _prune(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            DELETE FROM workflow_drafts
            WHERE id IN (
                SELECT id FROM workflow_drafts
                ORDER BY created_at DESC, rowid DESC
                LIMIT -1 OFFSET ?
            )
            """,
            (self.max_drafts,),
        )

    def _decrypt(self, payload: bytes) -> WorkflowDraft:
        try:
            return WorkflowDraft.model_validate_json(self._cipher.decrypt(payload))
        except (InvalidToken, ValueError) as exc:
            raise WorkflowStoreError("Workflow draft payload is unreadable") from exc

    def _migrate(self) -> None:
        with self._lock, private_sqlite_connection(self.database_path) as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version > WORKFLOW_SCHEMA_VERSION:
                raise WorkflowStoreError(
                    f"Workflow schema {version} is newer than supported schema "
                    f"{WORKFLOW_SCHEMA_VERSION}"
                )
            if version == 0:
                connection.execute(
                    """
                    CREATE TABLE workflow_drafts (
                        id TEXT PRIMARY KEY,
                        investigation_id TEXT NOT NULL,
                        destination TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        idempotency_key TEXT NOT NULL UNIQUE,
                        payload BLOB NOT NULL
                    )
                    """
                )
                connection.execute(
                    "CREATE INDEX workflow_created_at ON workflow_drafts(created_at DESC)"
                )
                connection.execute(f"PRAGMA user_version = {WORKFLOW_SCHEMA_VERSION}")
