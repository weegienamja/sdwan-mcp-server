"""Contracts for collaboration, identity, RBAC, and agent governance."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from cisco_vmanage_mcp.services.connectors import ConnectorDescriptor
from cisco_vmanage_mcp.services.governance import (
    AuthorizationError,
    ConflictError,
    GovernanceActor,
    GovernanceStore,
    build_role_view,
    default_read_only_agents,
)
from cisco_vmanage_mcp.services.investigations import Investigation

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


def _owner(actor_id: str = "owner-a", tenant_id: str = "tenant-a") -> GovernanceActor:
    return GovernanceActor(
        id=actor_id,
        tenant_id=tenant_id,
        display_name="Owner",
        role="owner",
    )


def test_collaboration_timeline_is_encrypted_private_and_revisioned(tmp_path) -> None:
    store = GovernanceStore(tmp_path / "state", bootstrap_owner=_owner())
    record = store.create_collaboration("investigation-1", "owner-a", now=NOW)

    commented = store.add_comment(
        record.investigation_id,
        "owner-a",
        "Investigating token=must-redact",
        expected_revision=record.revision,
        now=NOW + timedelta(minutes=1),
    )
    viewed = store.get_collaboration(record.investigation_id, "owner-a", now=NOW)

    assert commented.revision == 2
    assert viewed is not None
    assert viewed.events[-1].body == "Investigating token=***REDACTED***"
    assert b"Investigating" not in store.database_path.read_bytes()
    assert store.database_path.stat().st_mode & 0o777 == 0o600
    assert store.key_path.stat().st_mode & 0o777 == 0o600
    assert store.state_dir.stat().st_mode & 0o777 == 0o700


def test_tenant_isolation_and_role_policy(tmp_path) -> None:
    store = GovernanceStore(tmp_path / "state", bootstrap_owner=_owner())
    store.register_actor(
        "owner-a",
        GovernanceActor(
            id="operator-a",
            tenant_id="tenant-a",
            display_name="Operator A",
            role="operator",
        ),
    )
    store.register_actor(
        "owner-a",
        GovernanceActor(
            id="viewer-a",
            tenant_id="tenant-a",
            display_name="Viewer A",
            role="viewer",
        ),
    )
    store.register_actor(
        "owner-a",
        GovernanceActor(
            id="owner-b",
            tenant_id="tenant-b",
            display_name="Owner B",
            role="owner",
        ),
        allow_cross_tenant=True,
    )
    record = store.create_collaboration("investigation-1", "owner-a", now=NOW)

    updated = store.add_comment(
        record.investigation_id,
        "operator-a",
        "Checking BFD sessions",
        expected_revision=record.revision,
        now=NOW,
    )

    with pytest.raises(AuthorizationError):
        store.add_comment(
            record.investigation_id,
            "viewer-a",
            "Viewer cannot mutate",
            expected_revision=updated.revision,
            now=NOW,
        )
    with pytest.raises(AuthorizationError):
        store.get_collaboration(record.investigation_id, "owner-b", now=NOW)


def test_authenticated_human_identity_can_be_synchronized_but_not_retenanted(tmp_path) -> None:
    store = GovernanceStore(tmp_path / "state")
    operator = GovernanceActor(
        id="oidc-user",
        tenant_id="tenant-a",
        display_name="Network Operator",
        role="operator",
    )

    store.synchronize_authenticated_actor(operator)
    promoted = store.synchronize_authenticated_actor(
        operator.model_copy(update={"role": "owner"})
    )

    assert promoted.role == "owner"
    assert store.get_actor(operator.id).role == "owner"
    with pytest.raises(AuthorizationError, match="tenant"):
        store.synchronize_authenticated_actor(
            operator.model_copy(update={"tenant_id": "tenant-b"})
        )


def test_authenticated_agent_identity_is_rejected(tmp_path) -> None:
    store = GovernanceStore(tmp_path / "state")
    agent = GovernanceActor(
        id="external-agent",
        tenant_id="tenant-a",
        display_name="External Agent",
        role="agent",
        human_owner_id="owner-a",
    )

    with pytest.raises(AuthorizationError, match="human"):
        store.synchronize_authenticated_actor(agent)


def test_assignment_and_optimistic_conflicts(tmp_path) -> None:
    store = GovernanceStore(tmp_path / "state", bootstrap_owner=_owner())
    store.register_actor(
        "owner-a",
        GovernanceActor(
            id="operator-a",
            tenant_id="tenant-a",
            display_name="Operator A",
            role="operator",
        ),
    )
    record = store.create_collaboration("investigation-1", "owner-a", now=NOW)

    assigned = store.assign_owner(
        record.investigation_id,
        "owner-a",
        "operator-a",
        expected_revision=record.revision,
        now=NOW,
    )

    assert assigned.owner_id == "operator-a"
    with pytest.raises(ConflictError, match="revision"):
        store.add_comment(
            record.investigation_id,
            "operator-a",
            "Stale update",
            expected_revision=record.revision,
            now=NOW,
        )
    with pytest.raises(AuthorizationError):
        store.assign_owner(
            record.investigation_id,
            "operator-a",
            "owner-a",
            expected_revision=assigned.revision,
            now=NOW,
        )


def test_stale_evidence_is_flagged_at_read_time(tmp_path) -> None:
    store = GovernanceStore(tmp_path / "state", bootstrap_owner=_owner())
    record = store.create_collaboration("investigation-1", "owner-a", now=NOW)
    updated = store.add_evidence_reference(
        record.investigation_id,
        "owner-a",
        "GET /dataservice/device",
        observed_at=NOW,
        stale_after_seconds=60,
        expected_revision=record.revision,
        now=NOW,
    )

    fresh = store.get_collaboration(
        record.investigation_id,
        "owner-a",
        now=NOW + timedelta(seconds=30),
    )
    stale = store.get_collaboration(
        record.investigation_id,
        "owner-a",
        now=NOW + timedelta(seconds=61),
    )

    assert updated.revision == 2
    assert fresh.events[-1].stale is False
    assert stale.events[-1].stale is True


def test_competing_hypotheses_support_revisioned_confidence_changes(tmp_path) -> None:
    store = GovernanceStore(tmp_path / "state", bootstrap_owner=_owner())
    record = store.create_collaboration("investigation-1", "owner-a", now=NOW)
    first = store.add_hypothesis(
        record.investigation_id,
        "owner-a",
        "ISP path impairment",
        confidence="medium",
        expected_revision=record.revision,
        now=NOW,
    )
    second = store.add_hypothesis(
        record.investigation_id,
        "owner-a",
        "Branch router failure",
        confidence="low",
        expected_revision=first.revision,
        now=NOW,
    )
    updated = store.update_hypothesis(
        record.investigation_id,
        "owner-a",
        first.hypotheses[0].id,
        confidence="high",
        expected_revision=second.revision,
        now=NOW,
    )

    assert len(updated.hypotheses) == 2
    assert updated.hypotheses[0].confidence == "high"
    assert updated.hypotheses[1].confidence == "low"
    assert updated.events[-1].kind == "hypothesis"


def test_agent_registry_maps_owner_and_enforces_connector_policy(tmp_path) -> None:
    store = GovernanceStore(tmp_path / "state", bootstrap_owner=_owner())
    agent = GovernanceActor(
        id="assurance-agent",
        tenant_id="tenant-a",
        display_name="Assurance Agent",
        role="agent",
        human_owner_id="owner-a",
        allowed_connectors=("thousandeyes",),
        maximum_data_classification="restricted",
    )
    store.register_actor("owner-a", agent)
    thousandeyes = ConnectorDescriptor(
        id="thousandeyes",
        display_name="ThousandEyes",
        kind="assurance-mcp",
        enabled=True,
        timeout_ms=1_000,
        data_classification="restricted",
        capabilities=("alerts",),
        provenance="mcp://thousandeyes",
    )
    splunk = ConnectorDescriptor(
        id="splunk",
        display_name="Splunk",
        kind="https-json",
        enabled=True,
        timeout_ms=1_000,
        data_classification="sensitive",
        capabilities=("events",),
        provenance="https://splunk.example.test/evidence",
    )

    assert store.authorize_connector("assurance-agent", thousandeyes) is True
    assert store.authorize_connector("assurance-agent", splunk) is False
    assert store.get_actor("assurance-agent").human_owner_id == "owner-a"


def test_role_views_limit_detail_by_audience(tmp_path) -> None:
    store = GovernanceStore(tmp_path / "state", bootstrap_owner=_owner())
    collaboration = store.create_collaboration("investigation-1", "owner-a", now=NOW)
    investigation = Investigation.model_validate({
        "id": "investigation-1",
        "title": "Site 100 outage",
        "created_at": NOW,
        "updated_at": NOW,
        "messages": [
            {
                "role": "assistant",
                "content": "One edge is unreachable.",
                "created_at": NOW,
                "evidence": [],
            }
        ],
        "pins": [
            {
                "id": "device:1",
                "type": "device",
                "label": "edge-1",
                "data": {"system_ip": "10.0.0.1", "health": "critical"},
                "pinned_at": NOW,
            },
            {
                "id": "alarm:1",
                "type": "alarm",
                "label": "Control alarm",
                "data": {"severity": "critical"},
                "pinned_at": NOW,
            },
        ],
    })

    executive = build_role_view(investigation, collaboration, "executive")
    netops = build_role_view(investigation, collaboration, "netops")
    secops = build_role_view(investigation, collaboration, "secops")

    assert executive.summary == "One edge is unreachable."
    assert executive.messages == ()
    assert "10.0.0.1" not in str(executive.model_dump())
    assert netops.evidence[0]["data"]["system_ip"] == "10.0.0.1"
    assert [item["type"] for item in secops.evidence] == ["alarm"]


def test_default_agents_are_read_only_and_human_owned() -> None:
    agents = default_read_only_agents("owner-a", "tenant-a")

    assert {agent.id for agent in agents} == {
        "topology-agent",
        "assurance-agent",
        "security-agent",
        "change-readiness-agent",
    }
    assert all(agent.role == "agent" for agent in agents)
    assert all(agent.human_owner_id == "owner-a" for agent in agents)
    assert all(not any("write" in action for action in agent.allowed_actions) for agent in agents)
