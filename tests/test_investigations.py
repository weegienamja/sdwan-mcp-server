"""Contracts for bounded encrypted browser investigations."""

from __future__ import annotations

import json

from cisco_vmanage_mcp.services.investigations import InvestigationStore


def test_investigation_round_trip_is_encrypted_and_private(tmp_path) -> None:
    store = InvestigationStore(tmp_path / "state", max_investigations=10)

    investigation = store.create("Branch incident")
    store.append_message(investigation.id, "user", "Diagnose edge-1")
    store.append_message(
        investigation.id,
        "assistant",
        "edge-1 is critical",
        evidence=[{"label": "GET /dataservice/device", "state": "ok"}],
    )
    store.pin(
        investigation.id,
        {
            "id": "device:10.0.0.1",
            "type": "device",
            "label": "edge-1",
            "data": {"system_ip": "10.0.0.1", "health": "critical"},
        },
    )

    loaded = store.get(investigation.id)

    assert loaded is not None
    assert loaded.title == "Branch incident"
    assert [message.role for message in loaded.messages] == ["user", "assistant"]
    assert loaded.pins[0]["label"] == "edge-1"
    assert b"Diagnose edge-1" not in store.database_path.read_bytes()
    assert store.database_path.stat().st_mode & 0o777 == 0o600
    assert store.key_path.stat().st_mode & 0o777 == 0o600
    assert store.state_dir.stat().st_mode & 0o777 == 0o700


def test_investigation_retention_and_delete(tmp_path) -> None:
    store = InvestigationStore(tmp_path / "state", max_investigations=2)
    first = store.create("First")
    second = store.create("Second")
    third = store.create("Third")

    listed = store.list()

    assert [item.id for item in listed] == [third.id, second.id]
    assert store.get(first.id) is None
    assert store.delete(second.id) is True
    assert store.delete(second.id) is False


def test_pins_are_idempotent_and_removable(tmp_path) -> None:
    store = InvestigationStore(tmp_path / "state")
    investigation = store.create("Pinned evidence")
    pin = {"id": "source:device", "type": "source", "label": "Device API", "data": {}}

    store.pin(investigation.id, pin)
    store.pin(investigation.id, pin)

    loaded = store.get(investigation.id)
    assert loaded is not None
    assert len(loaded.pins) == 1
    assert store.unpin(investigation.id, pin["id"]) is True
    assert store.unpin(investigation.id, pin["id"]) is False


def test_exports_are_sanitized_and_machine_readable(tmp_path) -> None:
    store = InvestigationStore(tmp_path / "state")
    investigation = store.create("Export test")
    store.append_message(investigation.id, "user", "Assess fabric")
    store.pin(
        investigation.id,
        {
            "id": "source:device",
            "type": "source",
            "label": "GET /dataservice/device",
            "data": {"password": "do-not-export", "state": "ok"},
        },
    )

    markdown = store.export(investigation.id, "markdown")
    payload = json.loads(store.export(investigation.id, "json"))

    assert "Export test" in markdown
    assert "do-not-export" not in markdown
    assert "do-not-export" not in json.dumps(payload)
    assert payload["schema_version"] == 1
