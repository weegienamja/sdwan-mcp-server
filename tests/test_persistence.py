"""Contracts for shared encrypted persistence primitives."""

from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest

from cisco_vmanage_mcp.services.persistence import (
    canonical_json_bytes,
    canonical_sha256,
    load_or_create_fernet,
    prepare_private_directory,
    private_sqlite_connection,
)


class PersistenceError(RuntimeError):
    pass


def test_canonical_json_and_hash_ignore_mapping_order() -> None:
    first = {"site": 100, "state": "critical"}
    second = {"state": "critical", "site": 100}

    assert canonical_json_bytes(first) == canonical_json_bytes(second)
    assert canonical_sha256(first) == canonical_sha256(second)


def test_private_key_is_created_once_and_reused(tmp_path) -> None:
    state_dir = tmp_path / "state"
    key_path = state_dir / "state.key"
    prepare_private_directory(state_dir)

    first = load_or_create_fernet(
        key_path,
        error_type=PersistenceError,
        error_message="invalid key",
    )
    second = load_or_create_fernet(
        key_path,
        error_type=PersistenceError,
        error_message="invalid key",
    )

    encrypted = first.encrypt(b"evidence")
    assert second.decrypt(encrypted) == b"evidence"
    assert state_dir.stat().st_mode & 0o777 == 0o700
    assert key_path.stat().st_mode & 0o777 == 0o600


def test_invalid_key_uses_domain_error(tmp_path) -> None:
    state_dir = tmp_path / "state"
    key_path = state_dir / "state.key"
    prepare_private_directory(state_dir)
    key_path.write_text("invalid", encoding="ascii")

    with pytest.raises(PersistenceError, match="invalid key"):
        load_or_create_fernet(
            key_path,
            error_type=PersistenceError,
            error_message="invalid key",
        )


def test_private_sqlite_connection_commits_and_closes(tmp_path) -> None:
    database_path = tmp_path / "state.sqlite3"

    with private_sqlite_connection(database_path, foreign_keys=True) as connection:
        connection.execute("CREATE TABLE evidence (value TEXT NOT NULL)")
        connection.execute("INSERT INTO evidence VALUES ('stored')")
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1

    assert database_path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        connection.execute("SELECT 1")
    with closing(sqlite3.connect(database_path)) as verification:
        assert verification.execute("SELECT value FROM evidence").fetchone()[0] == "stored"
