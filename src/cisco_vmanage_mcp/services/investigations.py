"""Encrypted, bounded persistence for browser investigations."""

from __future__ import annotations

import html
import json
import re
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from cryptography.fernet import InvalidToken
from pydantic import BaseModel, ConfigDict, Field

from cisco_vmanage_mcp.services.persistence import (
    canonical_json_bytes,
    load_or_create_fernet,
    prepare_private_directory,
    private_sqlite_connection,
)

SCHEMA_VERSION = 1
DEFAULT_MAX_INVESTIGATIONS = 100
DEFAULT_MAX_MESSAGES = 200
DEFAULT_MAX_PINS = 100
REDACTED = "***REDACTED***"

_SENSITIVE_KEY_PARTS = (
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "jsessionid",
    "password",
    "passwd",
    "privatekey",
    "secret",
    "sessionid",
    "token",
)
_SENSITIVE_TEXT = re.compile(
    r"(?i)\b(api[_-]?key|authorization|password|passwd|secret|token)\b"
    r"(\s*[:=]\s*)([^\s,;]+)"
)


class InvestigationMessage(BaseModel):
    """A bounded conversation message and its cited evidence."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: str(uuid4()))
    role: Literal["user", "assistant", "system"]
    content: str = Field(min_length=1, max_length=20_000)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    evidence: list[dict[str, Any]] = Field(default_factory=list, max_length=50)


class PinnedEvidence(BaseModel):
    """A curated evidence item retained with an investigation."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=200)
    type: Literal["device", "alarm", "hypothesis", "source"]
    label: str = Field(min_length=1, max_length=500)
    data: dict[str, Any] = Field(default_factory=dict)
    pinned_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Investigation(BaseModel):
    """A durable investigation containing messages and selected evidence."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = SCHEMA_VERSION
    id: str = Field(default_factory=lambda: str(uuid4()))
    title: str = Field(min_length=1, max_length=200)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    messages: list[InvestigationMessage] = Field(default_factory=list)
    pins: list[dict[str, Any]] = Field(default_factory=list)


class InvestigationStoreError(RuntimeError):
    """Raised when persisted investigation data cannot be read safely."""


def _is_sensitive_key(key: object) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def sanitize_investigation_data(value: Any, depth: int = 0) -> Any:
    """Redact secrets and bound arbitrary evidence before storage or export."""
    if depth > 10:
        return "..."
    if isinstance(value, dict):
        return {
            str(key): (
                REDACTED
                if _is_sensitive_key(key)
                else sanitize_investigation_data(item, depth + 1)
            )
            for key, item in list(value.items())[:100]
        }
    if isinstance(value, (list, tuple)):
        return [sanitize_investigation_data(item, depth + 1) for item in value[:100]]
    if isinstance(value, str):
        redacted = _SENSITIVE_TEXT.sub(lambda match: f"{match.group(1)}{match.group(2)}{REDACTED}", value)
        return redacted[:20_000]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:2_000]


class InvestigationStore:
    """Store encrypted investigation documents in an owner-only SQLite database."""

    def __init__(
        self,
        state_dir: Path,
        *,
        max_investigations: int = DEFAULT_MAX_INVESTIGATIONS,
        max_messages: int = DEFAULT_MAX_MESSAGES,
        max_pins: int = DEFAULT_MAX_PINS,
    ) -> None:
        if not 1 <= max_investigations <= 1_000:
            raise ValueError("max_investigations must be between 1 and 1000")
        if not 1 <= max_messages <= 1_000:
            raise ValueError("max_messages must be between 1 and 1000")
        if not 1 <= max_pins <= 1_000:
            raise ValueError("max_pins must be between 1 and 1000")

        self.state_dir = Path(state_dir).expanduser()
        self.database_path = self.state_dir / "investigations.sqlite3"
        self.key_path = self.state_dir / "investigations.key"
        self.max_investigations = max_investigations
        self.max_messages = max_messages
        self.max_pins = max_pins
        self._lock = threading.RLock()

        prepare_private_directory(self.state_dir)
        self._cipher = load_or_create_fernet(
            self.key_path,
            error_type=InvestigationStoreError,
            error_message="Investigation encryption key is invalid",
        )
        self._migrate()

    def create(self, title: str = "New investigation") -> Investigation:
        """Create an investigation and prune records beyond the retention limit."""
        normalized_title = str(sanitize_investigation_data(title)).strip()
        investigation = Investigation(title=normalized_title or "New investigation")
        with self._lock, private_sqlite_connection(
            self.database_path,
            foreign_keys=True,
        ) as connection:
            self._write(connection, investigation)
            connection.execute(
                """
                DELETE FROM investigations
                WHERE id IN (
                    SELECT id FROM investigations
                    ORDER BY updated_at DESC, rowid DESC
                    LIMIT -1 OFFSET ?
                )
                """,
                (self.max_investigations,),
            )
        return investigation

    def list(self) -> list[Investigation]:
        """Return retained investigations, newest first."""
        with self._lock, private_sqlite_connection(
            self.database_path,
            foreign_keys=True,
        ) as connection:
            rows = connection.execute(
                "SELECT payload FROM investigations ORDER BY updated_at DESC, rowid DESC"
            ).fetchall()
        return [self._decrypt(row[0]) for row in rows]

    def get(self, investigation_id: str) -> Investigation | None:
        """Load one investigation by identifier."""
        with self._lock, private_sqlite_connection(
            self.database_path,
            foreign_keys=True,
        ) as connection:
            row = connection.execute(
                "SELECT payload FROM investigations WHERE id = ?",
                (investigation_id,),
            ).fetchone()
        return self._decrypt(row[0]) if row else None

    def delete(self, investigation_id: str) -> bool:
        """Delete one investigation and report whether it existed."""
        with self._lock, private_sqlite_connection(
            self.database_path,
            foreign_keys=True,
        ) as connection:
            cursor = connection.execute(
                "DELETE FROM investigations WHERE id = ?",
                (investigation_id,),
            )
        return cursor.rowcount > 0

    def append_message(
        self,
        investigation_id: str,
        role: Literal["user", "assistant", "system"],
        content: str,
        *,
        evidence: list[dict[str, Any]] | None = None,
    ) -> Investigation:
        """Append a sanitized message while retaining only the configured tail."""
        investigation = self._require(investigation_id)
        message = InvestigationMessage(
            role=role,
            content=str(sanitize_investigation_data(content)),
            evidence=sanitize_investigation_data(evidence or []),
        )
        investigation.messages = [*investigation.messages, message][-self.max_messages :]
        return self._save(investigation)

    def pin(self, investigation_id: str, item: dict[str, Any]) -> Investigation:
        """Add or replace a sanitized evidence pin by its stable identifier."""
        investigation = self._require(investigation_id)
        pin = PinnedEvidence.model_validate(sanitize_investigation_data(item))
        serialized = pin.model_dump(mode="json")
        investigation.pins = [
            existing for existing in investigation.pins if existing.get("id") != pin.id
        ]
        investigation.pins = [*investigation.pins, serialized][-self.max_pins :]
        return self._save(investigation)

    def unpin(self, investigation_id: str, pin_id: str) -> bool:
        """Remove one evidence pin and report whether it existed."""
        investigation = self._require(investigation_id)
        retained = [pin for pin in investigation.pins if pin.get("id") != pin_id]
        if len(retained) == len(investigation.pins):
            return False
        investigation.pins = retained
        self._save(investigation)
        return True

    def export(self, investigation_id: str, export_format: Literal["json", "markdown"]) -> str:
        """Export a sanitized investigation as JSON or inert Markdown."""
        investigation = self._require(investigation_id)
        payload = sanitize_investigation_data(investigation.model_dump(mode="json"))
        if export_format == "json":
            return json.dumps(payload, indent=2, sort_keys=True)
        if export_format != "markdown":
            raise ValueError("export_format must be 'json' or 'markdown'")
        return self._as_markdown(payload)

    def _migrate(self) -> None:
        with self._lock, private_sqlite_connection(
            self.database_path,
            foreign_keys=True,
        ) as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise InvestigationStoreError(
                    f"Investigation schema {version} is newer than supported schema {SCHEMA_VERSION}"
                )
            if version == 0:
                connection.execute(
                    """
                    CREATE TABLE investigations (
                        id TEXT PRIMARY KEY,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        payload BLOB NOT NULL
                    )
                    """
                )
                connection.execute(
                    "CREATE INDEX investigations_updated_at ON investigations(updated_at DESC)"
                )
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _write(
        self,
        connection: sqlite3.Connection,
        investigation: Investigation,
    ) -> None:
        payload = canonical_json_bytes(investigation.model_dump(mode="json"))
        encrypted = self._cipher.encrypt(payload)
        connection.execute(
            """
            INSERT INTO investigations (id, created_at, updated_at, payload)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                updated_at = excluded.updated_at,
                payload = excluded.payload
            """,
            (
                investigation.id,
                investigation.created_at.isoformat(),
                investigation.updated_at.isoformat(),
                encrypted,
            ),
        )

    def _save(self, investigation: Investigation) -> Investigation:
        investigation.updated_at = datetime.now(UTC)
        with self._lock, private_sqlite_connection(
            self.database_path,
            foreign_keys=True,
        ) as connection:
            self._write(connection, investigation)
        return investigation

    def _require(self, investigation_id: str) -> Investigation:
        investigation = self.get(investigation_id)
        if investigation is None:
            raise KeyError(f"Investigation not found: {investigation_id}")
        return investigation

    def _decrypt(self, payload: bytes) -> Investigation:
        try:
            decrypted = self._cipher.decrypt(payload)
            return Investigation.model_validate_json(decrypted)
        except (InvalidToken, ValueError) as exc:
            raise InvestigationStoreError("Investigation data could not be decrypted") from exc

    @staticmethod
    def _as_markdown(payload: dict[str, Any]) -> str:
        title = html.escape(str(payload["title"]), quote=False)
        lines = [f"# {title}", "", f"Updated: {payload['updated_at']}", ""]
        lines.extend(["## Conversation", ""])
        for message in payload["messages"]:
            role = str(message["role"]).title()
            content = html.escape(str(message["content"]), quote=False)
            lines.extend([f"### {role}", "", content, ""])
        lines.extend(["## Pinned evidence", ""])
        for pin in payload["pins"]:
            label = html.escape(str(pin["label"]), quote=False)
            pin_type = html.escape(str(pin["type"]), quote=False)
            lines.extend([f"### {label}", "", f"Type: {pin_type}", ""])
            serialized = json.dumps(pin["data"], indent=2, sort_keys=True)
            lines.extend(["    " + line for line in serialized.splitlines()])
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"
