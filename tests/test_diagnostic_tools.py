"""Safety-boundary tests for diagnostic MCP tools."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cisco_vmanage_mcp.services.health_check import (
    DataSource,
    FabricHealthReport,
)
from cisco_vmanage_mcp.tools import diagnostic_tools
from tests.test_retrieval_tools import DEVICE, RESPONSES


def _context():
    return SimpleNamespace(
        request_context=SimpleNamespace(lifespan_context={"vmanage": object()})
    )


class DiagnosticClient:
    def __init__(self, unhealthy: bool = False) -> None:
        self.unhealthy = unhealthy

    async def get(self, endpoint: str, params=None) -> dict:
        if endpoint == "/dataservice/device":
            controller = {
                **DEVICE,
                "host-name": "vmanage-1",
                "system-ip": "10.0.0.10",
                "deviceId": "10.0.0.10",
                "device-type": "vmanage",
                "device-model": "vmanage",
                "site-id": "1",
                "bfdSessions": "--",
                "controlConnections": 2,
            }
            edge = dict(DEVICE)
            if self.unhealthy:
                edge.update(
                    reachability="unreachable",
                    state="red",
                    bfdSessions=0,
                    controlConnections=0,
                )
            return {"data": [controller, edge]}
        if endpoint == "/dataservice/alarms/count":
            return {
                "data": [{"severity": "Critical", "count": 1 if self.unhealthy else 0}]
            }
        return RESPONSES[endpoint]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("report", "expected_blocker"),
    [
        (
            FabricHealthReport(
                timestamp=0,
                partial=True,
                incomplete_sources=[DataSource.ALARM_COUNT.value],
            ),
            "Assessment is incomplete",
        ),
        (
            FabricHealthReport(timestamp=0),
            "No devices were returned",
        ),
    ],
)
async def test_pre_change_validation_fails_closed(
    monkeypatch,
    report,
    expected_blocker,
) -> None:
    monkeypatch.setattr(
        diagnostic_tools,
        "assess_fabric_health",
        AsyncMock(return_value=report),
    )

    raw_result = await diagnostic_tools.vmanage_pre_change_validation(
        ctx=_context(),
        response_format="json",
    )
    result = json.loads(raw_result)

    assert result["recommendation"] == "NO-GO"
    assert any(expected_blocker in blocker for blocker in result["blockers"])


@pytest.mark.asyncio
@pytest.mark.parametrize("response_format", ["json", "markdown"])
async def test_assess_fabric_health_formats(response_format) -> None:
    result = await diagnostic_tools.vmanage_assess_fabric_health(
        ctx=SimpleNamespace(
            request_context=SimpleNamespace(
                lifespan_context={"vmanage": DiagnosticClient()}
            )
        ),
        response_format=response_format,
    )

    if response_format == "json":
        assert json.loads(result)["overall_health"] == "healthy"
    else:
        assert "Fabric Health: HEALTHY" in result


@pytest.mark.asyncio
@pytest.mark.parametrize("response_format", ["json", "markdown"])
async def test_diagnose_device_formats(response_format) -> None:
    result = await diagnostic_tools.vmanage_diagnose_device(
        system_ip="10.0.0.1",
        ctx=SimpleNamespace(
            request_context=SimpleNamespace(
                lifespan_context={"vmanage": DiagnosticClient(unhealthy=True)}
            )
        ),
        response_format=response_format,
    )

    if response_format == "json":
        assert json.loads(result)["device"]["health"] == "critical"
    else:
        assert "Device-Specific Analysis" in result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("unhealthy", "recommendation"),
    [(False, "GO"), (True, "NO-GO")],
)
async def test_pre_change_validation_health_decision(
    unhealthy,
    recommendation,
) -> None:
    result = await diagnostic_tools.vmanage_pre_change_validation(
        ctx=SimpleNamespace(
            request_context=SimpleNamespace(
                lifespan_context={"vmanage": DiagnosticClient(unhealthy)}
            )
        ),
        response_format="json",
    )

    assert json.loads(result)["recommendation"] == recommendation


@pytest.mark.asyncio
@pytest.mark.parametrize("audience", ["executive", "engineer"])
@pytest.mark.parametrize("response_format", ["json", "markdown"])
async def test_incident_summary_formats_and_audiences(
    audience,
    response_format,
) -> None:
    result = await diagnostic_tools.vmanage_incident_summary(
        ctx=SimpleNamespace(
            request_context=SimpleNamespace(
                lifespan_context={"vmanage": DiagnosticClient(unhealthy=True)}
            )
        ),
        audience=audience,
        response_format=response_format,
    )

    if response_format == "json":
        payload = json.loads(result)
        assert payload["audience"] == audience
        assert payload["overall_health"] == "critical"
    elif audience == "executive":
        assert "Fabric Status Summary" in result
    else:
        assert "Incident Report (Engineer)" in result
