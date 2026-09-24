"""Privacy and integration tests for optional telemetry."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from cisco_vmanage_mcp import telemetry
from cisco_vmanage_mcp.services import audit


def test_event_uses_random_installation_id_not_username(
    monkeypatch,
    tmp_path,
) -> None:
    installation_file = tmp_path / "telemetry" / "installation-id"
    monkeypatch.setattr(telemetry, "INSTALLATION_ID_FILE", installation_file, raising=False)
    monkeypatch.setenv("VMANAGE_USERNAME", "admin")

    first = telemetry._build_event("vmanage_list_devices", 10.0, True)
    monkeypatch.setenv("VMANAGE_USERNAME", "another-user")
    second = telemetry._build_event("vmanage_list_devices", 10.0, True)

    assert "user_hash" not in first
    assert first["installation_id"] == second["installation_id"]
    assert len(first["installation_id"]) == 64
    assert installation_file.stat().st_mode & 0o777 == 0o600
    assert installation_file.parent.stat().st_mode & 0o777 == 0o700


@pytest.mark.asyncio
@pytest.mark.parametrize("raises", [False, True])
async def test_audit_wrapper_emits_one_telemetry_event(
    monkeypatch,
    raises,
) -> None:
    events: list[tuple[str, bool]] = []
    monkeypatch.setattr(audit, "log_tool_call", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        telemetry,
        "emit_tool_event",
        lambda name, _duration, success: events.append((name, success)),
    )

    @audit.audit_tool("sample")
    async def sample_tool() -> str:
        if raises:
            raise RuntimeError("failed")
        return "ok"

    if raises:
        with pytest.raises(RuntimeError):
            await sample_tool()
    else:
        assert await sample_tool() == "ok"

    assert events == [("sample", not raises)]


@pytest.mark.asyncio
async def test_audit_wrapper_marks_error_result_as_failed(monkeypatch) -> None:
    events: list[bool] = []
    logged: dict = {}
    monkeypatch.setattr(
        audit,
        "log_tool_call",
        lambda *_args, **fields: logged.update(fields),
    )
    monkeypatch.setattr(
        telemetry,
        "emit_tool_event",
        lambda _name, _duration, success: events.append(success),
    )

    @audit.audit_tool("sample")
    async def sample_tool() -> str:
        return "Error: sensitive upstream detail"

    assert await sample_tool() == "Error: sensitive upstream detail"
    assert events == [False]
    assert logged["error"] == "tool_error"
    assert "sensitive" not in str(logged)


@pytest.mark.asyncio
async def test_ide_telemetry_is_disabled_by_default(monkeypatch) -> None:
    monkeypatch.delenv("VMANAGE_MCP_IDE_TELEMETRY", raising=False)
    monkeypatch.setattr(
        telemetry,
        "import_module",
        lambda _name: pytest.fail("SDK should not load while telemetry is disabled"),
        raising=False,
    )

    await telemetry.initialize_ide_telemetry()

    assert telemetry._ide_client is None


@pytest.mark.asyncio
async def test_ide_telemetry_uses_standalone_client(monkeypatch) -> None:
    calls: dict = {"events": []}

    class FakeTelemetryClient:
        @classmethod
        async def get_instance(cls, telemetry_config):
            calls["config"] = telemetry_config
            return cls()

        async def send_event(self, name, data, user_id=None) -> None:
            calls["events"].append((name, data, user_id))

        async def shutdown(self) -> None:
            calls["shutdown"] = True

    fake_module = SimpleNamespace(
        TelemetryClient=FakeTelemetryClient,
        TelemetryConfig=lambda **values: SimpleNamespace(**values),
    )
    monkeypatch.setenv("VMANAGE_MCP_IDE_TELEMETRY", "true")
    monkeypatch.setenv("IDE_MCP_MARKETPLACE_ID", "marketplace-123")
    monkeypatch.setattr(telemetry, "import_module", lambda _name: fake_module, raising=False)
    telemetry._ide_client = None

    await telemetry.initialize_ide_telemetry()
    await telemetry.emit_ide_tool_event("vmanage_list_devices", 12.5, True)
    await telemetry.shutdown_ide_telemetry()

    assert calls["config"].mcp_server_name == "cisco-vmanage-mcp"
    assert calls["config"].mcp_marketplace_id == "marketplace-123"
    assert calls["events"] == [
        (
            "vmanage_list_devices",
            {"durationMs": 12.5, "success": True},
            None,
        )
    ]
    assert calls["shutdown"] is True
