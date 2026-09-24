"""Shared owner-only persistence primitives for encrypted local stores."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from cryptography.fernet import Fernet


def canonical_json_bytes(value: object) -> bytes:
    """Serialize structured data deterministically for encryption or hashing."""
    return json.dumps(value, separators=(",", ":"), sort_keys=True, default=str).encode()


def canonical_sha256(value: object) -> str:
    """Return the SHA-256 digest of deterministic JSON data."""
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def prepare_private_directory(path: Path) -> None:
    """Create a state directory and restrict it to its owner where supported."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        path.chmod(0o700)


def load_or_create_fernet(
    key_path: Path,
    *,
    error_type: type[Exception],
    error_message: str,
) -> Fernet:
    """Load or atomically create an owner-only Fernet key."""
    try:
        descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        key = key_path.read_bytes().strip()
    else:
        key = Fernet.generate_key()
        with os.fdopen(descriptor, "wb") as key_file:
            key_file.write(key)
    if os.name != "nt":
        key_path.chmod(0o600)
    try:
        return Fernet(key)
    except (TypeError, ValueError) as exc:
        raise error_type(error_message) from exc


@contextmanager
def private_sqlite_connection(
    database_path: Path,
    *,
    foreign_keys: bool = False,
) -> Iterator[sqlite3.Connection]:
    """Yield a committed owner-only SQLite connection and always close it."""
    connection = sqlite3.connect(database_path, timeout=5)
    try:
        if foreign_keys:
            connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA secure_delete = ON")
        if os.name != "nt":
            database_path.chmod(0o600)
        with connection:
            yield connection
    finally:
        connection.close()
