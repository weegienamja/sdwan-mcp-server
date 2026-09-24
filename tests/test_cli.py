"""Command-level contracts for the Click CLI."""

from __future__ import annotations

import json
import time

from click.testing import CliRunner

from cisco_vmanage_mcp import cli as cli_module
from cisco_vmanage_mcp.client import AuthenticationError
from cisco_vmanage_mcp.client import PermissionError as VManagePermissionError


class FakeClient:
    """Minimal async vManage client for command tests."""

    instances: list[FakeClient] = []

    def __init__(self) -> None:
        self.host = "vmanage.example.test"
        self.port = "443"
        self.authenticated = False
        self.closed = False
        self.__class__.instances.append(self)

    async def authenticate(self) -> None:
        self.authenticated = True

    async def get(self, endpoint: str, params=None) -> dict:
        responses = {
            "/dataservice/device": [
                {
                    "host-name": "vmanage-1",
                    "system-ip": "10.0.0.10",
                    "deviceId": "10.0.0.10",
                    "device-type": "vmanage",
                    "device-model": "vmanage",
                    "reachability": "reachable",
                    "site-id": "1",
                    "version": "20.10.1",
                    "state": "green",
                    "bfdSessions": "--",
                    "controlConnections": 2,
                },
                {
                    "host-name": "edge-1",
                    "system-ip": "10.0.0.1",
                    "deviceId": "10.0.0.1",
                    "device-type": "vedge",
                    "device-model": "vedge-C8000V",
                    "reachability": "reachable",
                    "site-id": "100",
                    "version": "20.10.1",
                    "state": "green",
                    "bfdSessions": 8,
                    "controlConnections": 3,
                },
            ],
            "/dataservice/alarms": [
                {
                    "severity": "Critical",
                    "type": "Control",
                    "host_name": "edge-1",
                    "system_ip": "10.0.0.1",
                    "message": "Control connection changed",
                    "entry_time": int(time.time() * 1000),
                },
                {
                    "severity": "Minor",
                    "type": "System",
                    "host_name": "vmanage-1",
                    "system_ip": "10.0.0.10",
                    "message": "Informational",
                    "entry_time": int(time.time() * 1000),
                },
            ],
            "/dataservice/alarms/count": [
                {"severity": "Critical", "count": 0},
                {"severity": "Major", "count": 0},
            ],
            "/dataservice/device/bfd/sessions": [],
            "/dataservice/device/control/connections": [],
            "/dataservice/device/system/status": [
                {"min5_avg": 10, "mem_used": 10, "mem_free": 90}
            ],
        }
        return {"data": responses[endpoint]}

    async def close(self) -> None:
        self.closed = True


def test_status_json_uses_client_and_closes(monkeypatch) -> None:
    FakeClient.instances.clear()
    monkeypatch.setattr(cli_module, "VManageClient", FakeClient)

    result = CliRunner().invoke(cli_module.cli, ["--json", "status"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["connected"] is True
    assert payload["device_count"] == 2
    assert payload["controllers"] == 1
    assert payload["edges"] == 1
    assert FakeClient.instances[0].authenticated is True
    assert FakeClient.instances[0].closed is True


def test_devices_json_applies_filters(monkeypatch) -> None:
    FakeClient.instances.clear()
    monkeypatch.setattr(cli_module, "VManageClient", FakeClient)

    result = CliRunner().invoke(
        cli_module.cli,
        [
            "--json",
            "devices",
            "--type",
            "vedge",
            "--reachability",
            "reachable",
            "--limit",
            "1",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert [device["hostname"] for device in payload] == ["edge-1"]
    assert FakeClient.instances[0].closed is True


def test_alarms_json_applies_filters(monkeypatch) -> None:
    FakeClient.instances.clear()
    monkeypatch.setattr(cli_module, "VManageClient", FakeClient)

    result = CliRunner().invoke(
        cli_module.cli,
        ["--json", "alarms", "--severity", "Critical", "--hours", "1", "--limit", "1"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert len(payload) == 1
    assert payload[0]["severity"] == "Critical"
    assert FakeClient.instances[0].closed is True


def test_health_json_runs_real_correlation(monkeypatch) -> None:
    FakeClient.instances.clear()
    monkeypatch.setattr(cli_module, "VManageClient", FakeClient)

    result = CliRunner().invoke(cli_module.cli, ["--json", "health"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["overall_health"] == "healthy"
    assert payload["device_count"] == 2
    assert payload["impact"]["scope"] == "none"
    assert FakeClient.instances[0].closed is True


def test_diagnose_json_runs_real_service(monkeypatch) -> None:
    FakeClient.instances.clear()
    monkeypatch.setattr(cli_module, "VManageClient", FakeClient)

    result = CliRunner().invoke(
        cli_module.cli,
        ["--json", "diagnose", "10.0.0.1"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["system_ip"] == "10.0.0.1"
    assert payload["device"]["hostname"] == "edge-1"
    assert payload["fabric_health"] == "healthy"
    assert FakeClient.instances[0].closed is True


def test_smoke_test_json_runs_all_checks(monkeypatch) -> None:
    FakeClient.instances.clear()
    monkeypatch.setattr(cli_module, "VManageClient", FakeClient)

    result = CliRunner().invoke(cli_module.cli, ["--json", "smoke-test"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["overall"] == "pass"
    assert [check["check"] for check in payload["checks"]] == [
        "authentication",
        "device_api",
        "alarm_api",
    ]
    assert FakeClient.instances[0].closed is True


def test_qualify_json_reports_customer_capabilities(monkeypatch) -> None:
    FakeClient.instances.clear()
    monkeypatch.setattr(cli_module, "VManageClient", FakeClient)

    result = CliRunner().invoke(cli_module.cli, ["--json", "qualify", "--concurrency", "2"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ready"] is True
    assert payload["manager"] == "vmanage.example.test:443"
    assert payload["device_count"] == 2
    assert payload["software_versions"] == {"20.10.1": 2}
    assert payload["capabilities"][0]["id"] == "inventory"
    assert FakeClient.instances[0].closed is True


def test_qualify_text_formats_partial_environment(monkeypatch) -> None:
    class PartialClient(FakeClient):
        async def get(self, endpoint: str, params=None) -> dict:
            if endpoint == "/dataservice/template/device":
                raise VManagePermissionError("denied", 403)
            return await super().get(endpoint, params=params)

    PartialClient.instances.clear()
    monkeypatch.setattr(cli_module, "VManageClient", PartialClient)

    result = CliRunner().invoke(cli_module.cli, ["qualify"])

    assert result.exit_code == 0, result.output
    assert "READY (PARTIAL CAPABILITY SUPPORT)" in result.output
    assert "[forbidden] device-templates (permission-denied)" in result.output


def test_qualify_exits_nonzero_when_required_inventory_is_forbidden(monkeypatch) -> None:
    class ForbiddenClient(FakeClient):
        async def get(self, endpoint: str, params=None) -> dict:
            if endpoint == "/dataservice/device":
                raise VManagePermissionError("denied", 403)
            return await super().get(endpoint, params=params)

    ForbiddenClient.instances.clear()
    monkeypatch.setattr(cli_module, "VManageClient", ForbiddenClient)

    result = CliRunner().invoke(cli_module.cli, ["--json", "qualify"])

    assert result.exit_code == 1
    assert json.loads(result.output)["ready"] is False
    assert ForbiddenClient.instances[0].closed is True


def test_qualify_reports_client_configuration_errors(monkeypatch) -> None:
    class InvalidClient:
        def __init__(self) -> None:
            raise AuthenticationError("credentials missing")

    monkeypatch.setattr(cli_module, "VManageClient", InvalidClient)

    result = CliRunner().invoke(cli_module.cli, ["--json", "qualify"])

    assert result.exit_code == 1
    assert json.loads(result.output) == {"error": "credentials missing"}


def test_status_failure_is_json_and_closes(monkeypatch) -> None:
    class FailingClient(FakeClient):
        async def authenticate(self) -> None:
            raise AuthenticationError("bad credentials")

    FailingClient.instances.clear()
    monkeypatch.setattr(cli_module, "VManageClient", FailingClient)

    result = CliRunner().invoke(cli_module.cli, ["--json", "status"])

    assert result.exit_code == 1
    assert json.loads(result.output) == {"error": "bad credentials"}
    assert FailingClient.instances[0].closed is True


def test_text_formatters_cover_empty_and_populated_results() -> None:
    assert cli_module._format_devices([]) == "No devices found."
    assert cli_module._format_alarms([]) == "No active alarms."
    status = cli_module._format_status(
        {
            "host": "vmanage.example.test",
            "port": "443",
            "device_count": 2,
            "controllers": 1,
            "edges": 1,
            "response_time_ms": 10,
        }
    )
    assert "vManage Connection: OK" in status
