"""Contract tests for user-facing entry points."""

import json
from types import SimpleNamespace

from cisco_vmanage_mcp import ai_providers, app
from cisco_vmanage_mcp.cli import _diagnosis_to_dict


def _diagnosis_result() -> tuple[SimpleNamespace, SimpleNamespace]:
    device = SimpleNamespace(
        hostname="edge-1",
        system_ip="10.0.0.1",
        site_id="100",
        reachable=False,
        overall_health=SimpleNamespace(value="critical"),
        bfd_sessions=0,
        control_connections=0,
    )
    correlation = SimpleNamespace(
        fabric_report=SimpleNamespace(overall_health=SimpleNamespace(value="critical")),
        impact=None,
        root_causes=[],
        narrative="Device is isolated.",
    )
    return correlation, device


def test_cli_diagnosis_unpacks_service_result() -> None:
    result = _diagnosis_to_dict(_diagnosis_result())

    assert result["system_ip"] == "10.0.0.1"
    assert result["device"]["hostname"] == "edge-1"
    assert result["fabric_health"] == "critical"
    assert result["narrative"] == "Device is isolated."


def test_app_diagnosis_unpacks_service_result(monkeypatch, capsys) -> None:
    def fake_run(coroutine):
        coroutine.close()
        return _diagnosis_result()

    monkeypatch.setattr(app, "_run", fake_run)
    monkeypatch.setattr(app, "_spin_print", lambda _message: None)

    app._cmd_diagnose("10.0.0.1")

    output = capsys.readouterr().out
    assert "edge-1 (10.0.0.1)" in output
    assert "Device is isolated." in output


def test_ai_tool_diagnosis_unpacks_service_result(monkeypatch) -> None:
    def fake_run(coroutine):
        coroutine.close()
        return _diagnosis_result()

    monkeypatch.setattr(ai_providers, "_app_run", fake_run)

    result = json.loads(ai_providers._tool_diagnose("10.0.0.1"))

    assert result["device"]["hostname"] == "edge-1"
    assert result["fabric_health"] == "critical"
    assert result["narrative"] == "Device is isolated."
