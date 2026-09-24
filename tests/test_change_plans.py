"""Contracts for guarded change planning without live execution."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from cisco_vmanage_mcp.services.change_plans import (
    ChangePlanStore,
    ExecutionDisabledError,
)
from cisco_vmanage_mcp.services.governance import (
    ActorRole,
    AuthorizationError,
    GovernanceActor,
)
from tests.test_snapshots import _snapshot

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


def _actor(
    actor_id: str = "owner-a",
    tenant_id: str = "tenant-a",
    role: ActorRole = "owner",
) -> GovernanceActor:
    return GovernanceActor(
        id=actor_id,
        tenant_id=tenant_id,
        display_name=actor_id,
        role=role,
    )


def test_plan_is_immutable_encrypted_and_canary_scoped(tmp_path) -> None:
    store = ChangePlanStore(tmp_path / "state")
    plan = store.create(
        actor=_actor(),
        operation="attach-device-template",
        intent="Roll out branch template",
        target_id="template-1",
        targets=("edge-1", "edge-2"),
        canary_targets=("edge-1",),
        pre_snapshot_id="snapshot-before",
        parameters={"template_uuid": "template-1", "password": "must-redact"},
        rollback_strategy="Reattach the previous approved template.",
        now=NOW,
    )

    assert plan.status == "planned"
    assert len(plan.plan_hash) == 64
    assert plan.parameters["password"] == "***REDACTED***"
    assert plan.canary_targets == ("edge-1",)
    assert plan.execution_enabled is False
    assert b"Roll out branch template" not in store.database_path.read_bytes()
    assert store.database_path.stat().st_mode & 0o777 == 0o600
    assert store.key_path.stat().st_mode & 0o777 == 0o600

    with pytest.raises(ValueError, match="subset"):
        store.create(
            actor=_actor(),
            operation="attach-device-template",
            intent="Invalid canary",
            target_id="template-1",
            targets=("edge-1",),
            canary_targets=("edge-2",),
            pre_snapshot_id="snapshot-before",
            now=NOW,
        )


def test_approval_is_human_exact_hash_and_short_lived(tmp_path) -> None:
    store = ChangePlanStore(tmp_path / "state")
    plan = store.create(
        actor=_actor(role="operator"),
        operation="attach-device-template",
        intent="Canary template rollout",
        target_id="template-1",
        targets=("edge-1",),
        canary_targets=("edge-1",),
        pre_snapshot_id="snapshot-before",
        now=NOW,
    )
    owner = _actor()

    with pytest.raises(ValueError, match="plan hash"):
        store.approve(plan.id, actor=owner, expected_hash="wrong", now=NOW)
    with pytest.raises(AuthorizationError):
        store.approve(
            plan.id,
            actor=GovernanceActor(
                id="agent-a",
                tenant_id="tenant-a",
                display_name="Agent A",
                role="agent",
                human_owner_id="owner-a",
            ),
            expected_hash=plan.plan_hash,
            now=NOW,
        )

    approved = store.approve(
        plan.id,
        actor=owner,
        expected_hash=plan.plan_hash,
        approval_minutes=15,
        now=NOW,
    )

    assert approved.status == "approved"
    assert approved.plan_hash == plan.plan_hash
    assert approved.approval is not None
    assert approved.approval.expires_at == NOW + timedelta(minutes=15)


def test_verification_recommends_rollback_on_regression(tmp_path) -> None:
    store = ChangePlanStore(tmp_path / "state")
    plan = store.create(
        actor=_actor(),
        operation="attach-device-template",
        intent="Canary template rollout",
        target_id="template-1",
        targets=("edge-1",),
        canary_targets=("edge-1",),
        pre_snapshot_id="before",
        rollback_strategy="Restore template-previous.",
        now=NOW,
    )
    approved = store.approve(
        plan.id,
        actor=_actor(),
        expected_hash=plan.plan_hash,
        now=NOW,
    )
    before = _snapshot("before", NOW - timedelta(minutes=5))
    after = _snapshot(
        "after",
        NOW + timedelta(minutes=5),
        reachable=False,
        critical_alarms=1,
        bfd_down=1,
    )

    verified = store.verify(
        approved.id,
        actor=_actor(),
        before=before,
        after=after,
        now=NOW + timedelta(minutes=5),
    )

    assert verified.status == "verification-failed"
    assert verified.verification is not None
    assert verified.verification.comparison.verdict == "regressed"
    assert verified.verification.rollback_recommended is True
    assert verified.verification.rollback_strategy == "Restore template-previous."


def test_expired_and_cross_tenant_plans_cannot_progress(tmp_path) -> None:
    store = ChangePlanStore(tmp_path / "state")
    plan = store.create(
        actor=_actor(),
        operation="activate-central-policy",
        intent="Activate approved policy",
        target_id="policy-1",
        targets=("fabric",),
        canary_targets=(),
        pre_snapshot_id="before",
        ttl_minutes=5,
        now=NOW,
    )

    with pytest.raises(AuthorizationError):
        store.get(plan.id, actor=_actor("owner-b", "tenant-b"))
    with pytest.raises(ValueError, match="expired"):
        store.approve(
            plan.id,
            actor=_actor(),
            expected_hash=plan.plan_hash,
            now=NOW + timedelta(minutes=6),
        )


def test_execution_is_structurally_disabled(tmp_path) -> None:
    store = ChangePlanStore(tmp_path / "state")
    plan = store.create(
        actor=_actor(),
        operation="attach-device-template",
        intent="No live write",
        target_id="template-1",
        targets=("edge-1",),
        canary_targets=("edge-1",),
        pre_snapshot_id="before",
        now=NOW,
    )

    assert not hasattr(store, "apply")
    with pytest.raises(ExecutionDisabledError):
        store.assert_execution_enabled(plan.id)
