"""Contracts for connector isolation and external evidence normalization."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from cisco_vmanage_mcp.services.connectors import (
    ConnectorDescriptor,
    ConnectorEvidence,
    ConnectorRegistry,
    HTTPJSONConnector,
    ThousandEyesConnector,
    build_default_registry,
)


class FakeConnector:
    def __init__(
        self,
        connector_id: str,
        *,
        enabled: bool = True,
        fail: bool = False,
        delay: float = 0,
    ) -> None:
        self.fail = fail
        self.delay = delay
        self.descriptor = ConnectorDescriptor(
            id=connector_id,
            display_name=connector_id.title(),
            kind="test",
            enabled=enabled,
            timeout_ms=10,
            data_classification="operational",
            capabilities=("alerts",),
            provenance=f"test://{connector_id}",
        )

    async def collect(self, capability: str, context: dict) -> list[ConnectorEvidence]:
        await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("token=connector-secret")
        return [
            ConnectorEvidence(
                id=f"{self.descriptor.id}:1",
                connector_id=self.descriptor.id,
                capability=capability,
                title="External alert",
                severity="major",
                data={"site_id": context.get("site_id"), "api_key": "must-redact"},
                provenance=self.descriptor.provenance,
            )
        ]


@pytest.mark.asyncio
async def test_registry_isolates_failures_timeouts_and_disabled_connectors() -> None:
    registry = ConnectorRegistry()
    registry.register(FakeConnector("working"))
    registry.register(FakeConnector("failing", fail=True))
    registry.register(FakeConnector("slow", delay=0.1))
    registry.register(FakeConnector("disabled", enabled=False))

    result = await registry.collect("alerts", {"site_id": "100"})

    assert result.partial is True
    assert [item.connector_id for item in result.items] == ["working"]
    assert result.items[0].data["api_key"] == "***REDACTED***"
    states = {source.connector_id: source.state for source in result.sources}
    assert states == {
        "disabled": "disabled",
        "failing": "failed",
        "slow": "failed",
        "working": "ok",
    }
    assert "connector-secret" not in str(result.model_dump())


def test_registry_rejects_duplicate_connector_ids() -> None:
    registry = ConnectorRegistry()
    registry.register(FakeConnector("duplicate"))

    with pytest.raises(ValueError, match="already registered"):
        registry.register(FakeConnector("duplicate"))


@pytest.mark.asyncio
async def test_registry_can_restrict_collection_to_policy_approved_ids() -> None:
    registry = ConnectorRegistry()
    registry.register(FakeConnector("allowed"))
    registry.register(FakeConnector("blocked"))

    result = await registry.collect("alerts", connector_ids={"allowed"})

    assert [item.connector_id for item in result.items] == ["allowed"]
    assert [source.connector_id for source in result.sources] == ["allowed"]


@pytest.mark.asyncio
async def test_thousandeyes_adapter_maps_capabilities_without_exposing_token() -> None:
    class ToolCaller:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        async def call_tool(self, name: str, arguments: dict) -> dict:
            self.calls.append((name, arguments))
            return {
                "alerts": [
                    {
                        "id": "alert-1",
                        "alertType": "Path loss",
                        "severity": "critical",
                        "siteId": "100",
                    }
                ]
            }

    caller = ToolCaller()
    connector = ThousandEyesConnector(caller=caller, enabled=True)

    evidence = await connector.collect("alerts", {"window_hours": 24})

    assert caller.calls == [(connector.alert_tool, {"window_hours": 24})]
    assert evidence[0].id == "alert-1"
    assert evidence[0].title == "Path loss"
    assert evidence[0].provenance.startswith("mcp://thousandeyes/")
    assert "token" not in connector.descriptor.model_dump()


def test_default_registry_requires_explicit_thousandeyes_configuration(monkeypatch) -> None:
    monkeypatch.delenv("THOUSANDEYES_MCP_URL", raising=False)
    monkeypatch.delenv("THOUSANDEYES_MCP_TOKEN", raising=False)
    monkeypatch.delenv("SPLUNK_EVIDENCE_URL", raising=False)
    monkeypatch.delenv("SPLUNK_EVIDENCE_TOKEN", raising=False)
    monkeypatch.delenv("APPDYNAMICS_EVIDENCE_URL", raising=False)
    monkeypatch.delenv("APPDYNAMICS_EVIDENCE_TOKEN", raising=False)

    registry = build_default_registry()
    descriptors = {item.id: item for item in registry.describe()}

    assert descriptors["vmanage"].enabled is True
    assert descriptors["thousandeyes"].enabled is False
    assert descriptors["thousandeyes"].state == "unconfigured"
    assert descriptors["splunk"].enabled is False
    assert descriptors["appdynamics"].enabled is False


def test_invalid_optional_connector_url_degrades_without_breaking_registry(monkeypatch) -> None:
    monkeypatch.setenv("SPLUNK_EVIDENCE_URL", "http://insecure.example.test/evidence")
    monkeypatch.setenv("SPLUNK_EVIDENCE_TOKEN", "private-token")

    registry = build_default_registry()
    descriptors = {item.id: item for item in registry.describe()}

    assert descriptors["vmanage"].enabled is True
    assert descriptors["splunk"].enabled is False
    assert descriptors["splunk"].state == "unconfigured"


@pytest.mark.asyncio
async def test_https_json_connector_sends_private_auth_and_normalizes_evidence() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer test-token"
        assert request.url.params["site_id"] == "100"
        assert "ignored" not in request.url.params
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "event-1",
                        "title": "Application latency",
                        "severity": "major",
                        "password": "must-redact",
                    }
                ]
            },
        )

    connector = HTTPJSONConnector(
        connector_id="appdynamics",
        display_name="Cisco AppDynamics",
        capability="application-evidence",
        url="https://appd.example.test/evidence",
        access_credential="test-token",
        handoff_url="https://appd.example.test/controller/applications",
        transport=httpx.MockTransport(handler),
    )

    evidence = await connector.collect(
        "application-evidence",
        {"site_id": "100", "ignored": "value"},
    )

    assert evidence[0].id == "event-1"
    assert evidence[0].title == "Application latency"
    assert evidence[0].data["password"] == "***REDACTED***"
    assert "test-token" not in str(connector.descriptor.model_dump())
    assert connector.descriptor.handoff_url == "https://appd.example.test/controller/applications"
