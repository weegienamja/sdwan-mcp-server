"""Contracts for preview-only external workflow drafts and approvals."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from cisco_vmanage_mcp.services.investigations import Investigation
from cisco_vmanage_mcp.services.workflows import WorkflowStore

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


def _investigation() -> Investigation:
    return Investigation.model_validate({
        "id": "investigation-1",
        "title": "Site 100 control outage",
        "created_at": NOW,
        "updated_at": NOW,
        "messages": [
            {
                "role": "assistant",
                "content": "Site 100 has lost control connectivity.",
                "created_at": NOW,
                "evidence": [{"label": "GET /dataservice/device", "state": "ok"}],
            }
        ],
        "pins": [
            {
                "id": "alarm:1",
                "type": "alarm",
                "label": "Control connection",
                "data": {"site_id": "100", "password": "must-redact"},
                "pinned_at": NOW,
            }
        ],
    })


@pytest.mark.parametrize("destination", ["servicenow", "webex", "slack"])
def test_workflow_drafts_are_sanitized_immutable_and_idempotent(tmp_path, destination) -> None:
    store = WorkflowStore(tmp_path / "state")

    first = store.create_draft(
        _investigation(),
        destination,
        target="noc-primary",
        actor_id="local-operator",
        now=NOW,
    )
    repeated = store.create_draft(
        _investigation(),
        destination,
        target="noc-primary",
        actor_id="local-operator",
        now=NOW + timedelta(minutes=1),
    )

    assert first.id == repeated.id
    assert first.status == "draft"
    assert len(first.content_hash) == 64
    assert len(first.idempotency_key) == 64
    assert "must-redact" not in str(first.model_dump())
    assert first.preview
    assert b"Site 100 control outage" not in store.database_path.read_bytes()
    assert store.database_path.stat().st_mode & 0o777 == 0o600
    assert store.key_path.stat().st_mode & 0o777 == 0o600


def test_workflow_approval_is_bound_to_exact_content_hash(tmp_path) -> None:
    store = WorkflowStore(tmp_path / "state")
    draft = store.create_draft(_investigation(), "servicenow", now=NOW)

    with pytest.raises(ValueError, match="content hash"):
        store.approve(
            draft.id,
            actor_id="operator-1",
            expected_hash="wrong",
            now=NOW + timedelta(minutes=5),
        )

    approved = store.approve(
        draft.id,
        actor_id="operator-1",
        expected_hash=draft.content_hash,
        now=NOW + timedelta(minutes=5),
    )
    repeated = store.approve(
        draft.id,
        actor_id="operator-1",
        expected_hash=draft.content_hash,
        now=NOW + timedelta(minutes=6),
    )

    assert approved.status == "approved"
    assert repeated == approved
    assert len(approved.approvals) == 1
    assert [event.action for event in approved.audit_events] == ["drafted", "approved"]


def test_expired_or_cancelled_drafts_cannot_be_approved(tmp_path) -> None:
    store = WorkflowStore(tmp_path / "state")
    expired = store.create_draft(_investigation(), "slack", ttl_minutes=5, now=NOW)
    cancelled = store.create_draft(
        _investigation(),
        "webex",
        target="noc-room",
        now=NOW,
    )
    store.cancel(cancelled.id, actor_id="operator-2", now=NOW + timedelta(minutes=1))

    with pytest.raises(ValueError, match="expired"):
        store.approve(
            expired.id,
            actor_id="operator-1",
            expected_hash=expired.content_hash,
            now=NOW + timedelta(minutes=6),
        )
    with pytest.raises(ValueError, match="cancelled"):
        store.approve(
            cancelled.id,
            actor_id="operator-1",
            expected_hash=cancelled.content_hash,
            now=NOW + timedelta(minutes=2),
        )


def test_workflow_store_has_no_delivery_operation(tmp_path) -> None:
    store = WorkflowStore(tmp_path / "state")

    assert not hasattr(store, "send")
    assert not hasattr(store, "deliver")
