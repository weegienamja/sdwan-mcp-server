"""Offline behavior tests for the interactive console."""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from types import SimpleNamespace

import pytest

from cisco_vmanage_mcp import app


class ConsoleClient:
    def __init__(self, fail: bool = False) -> None:
        self.host = "vmanage.example.test"
        self.port = "443"
        self._token = "token"
        self.fail = fail
        self.closed = False

    async def authenticate(self) -> None:
        if self.fail:
            raise RuntimeError("authentication failed")
        self._token = "token"

    async def get(self, endpoint: str, params=None) -> dict:
        if self.fail:
            raise RuntimeError("endpoint unavailable")
        now = int(time.time() * 1000)
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
                    "reachability": "unreachable",
                    "site-id": "100",
                    "state": "red",
                    "bfdSessions": 0,
                    "controlConnections": 0,
                },
            ],
            "/dataservice/alarms": [
                {
                    "severity": "Critical",
                    "type": "Control",
                    "host_name": "edge-1",
                    "system_ip": "10.0.0.1",
                    "message": "Control changed",
                    "entry_time": now,
                }
            ],
            "/dataservice/alarms/count": [
                {"severity": "Critical", "count": 1},
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


def _run_coroutine(coroutine):
    return asyncio.run(coroutine)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("devices", ("devices", [])),
        ("diagnose 10.0.0.1", ("diagnose", ["10.0.0.1"])),
        ("What is the fabric health?", ("health", ["is", "the", "fabric", "health?"])),
        ("Any critical alarms?", ("alarms", ["critical", "alarms?"])),
        ("Investigate 10.0.0.9", ("diagnose", ["10.0.0.9"])),
        ("run a quick check", ("smoke-test", ["a", "quick", "check"])),
        ("are we connected?", ("status", ["we", "connected?"])),
        ("something unrelated", (None, [])),
    ],
)
def test_match_intent(text, expected) -> None:
    assert app._match_intent(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("status", ("status", [])),
        ("diagnose 10.0.0.1", ("diagnose", ["10.0.0.1"])),
        ("show devices", (None, [])),
        ("", (None, [])),
    ],
)
def test_exact_command(text, expected) -> None:
    assert app._is_exact_command(text) == expected


@pytest.mark.parametrize(
    ("command", "arguments", "target"),
    [
        ("status", [], "_cmd_status"),
        ("devices", [], "_cmd_devices"),
        ("health", [], "_cmd_health"),
        ("alarms", [], "_cmd_alarms"),
        ("diagnose", ["10.0.0.1"], "_cmd_diagnose"),
        ("smoke-test", [], "_cmd_smoke_test"),
    ],
)
def test_run_cli_command_dispatches(monkeypatch, command, arguments, target) -> None:
    calls: list[tuple] = []
    for name in (
        "_cmd_status",
        "_cmd_devices",
        "_cmd_health",
        "_cmd_alarms",
        "_cmd_diagnose",
        "_cmd_smoke_test",
    ):
        monkeypatch.setattr(
            app,
            name,
            lambda *args, _name=name: calls.append((_name, *args)),
        )

    app._run_cli_command(command, arguments)

    expected_arguments = tuple(arguments) if target == "_cmd_diagnose" else ()
    assert calls == [(target, *expected_arguments)]


def test_run_cli_command_shows_diagnose_usage(capsys) -> None:
    app._run_cli_command("diagnose", [])

    assert "Usage:" in capsys.readouterr().out


def test_run_cli_command_resets_expired_session(monkeypatch, capsys) -> None:
    sentinel = object()
    monkeypatch.setattr(app, "_shared_client", sentinel)
    monkeypatch.setattr(
        app,
        "_cmd_status",
        lambda: (_ for _ in ()).throw(RuntimeError("503 auth token expired")),
    )

    app._run_cli_command("status", [])

    assert app._shared_client is None
    assert "Session expired" in capsys.readouterr().out


def test_suppress_noisy_logs_preserves_audit_file_logging(tmp_path) -> None:
    audit_logger = logging.getLogger("cisco_vmanage_mcp.audit")
    original_handlers = audit_logger.handlers[:]
    original_level = audit_logger.level
    file_handler = logging.FileHandler(tmp_path / "audit.log")
    stream_handler = logging.StreamHandler(sys.stderr)
    audit_logger.handlers[:] = [file_handler, stream_handler]
    audit_logger.setLevel(logging.INFO)
    try:
        app._suppress_noisy_logs()

        assert file_handler in audit_logger.handlers
        assert stream_handler not in audit_logger.handlers
        assert audit_logger.isEnabledFor(logging.INFO)
    finally:
        file_handler.close()
        audit_logger.handlers[:] = original_handlers
        audit_logger.setLevel(original_level)


def test_console_output_helpers(capsys) -> None:
    assert app._c("text", app.GREEN).endswith(app.RESET)
    app._clear_screen()
    app._step_ok("ready", "detail")
    app._step_warn("warning")
    app._step_fail("failed")

    output = capsys.readouterr().out
    assert "\033[2J\033[H" in output
    assert "ready" in output
    assert "warning" in output
    assert "failed" in output


def test_spinner_uses_frames(monkeypatch, capsys) -> None:
    values = iter([0.0, 0.0, 1.0])
    monkeypatch.setattr(app.time, "monotonic", lambda: next(values))
    monkeypatch.setattr(app.time, "sleep", lambda _seconds: None)

    app._spin_print("working", duration=0.5)

    assert "working" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_console_network_helpers(monkeypatch) -> None:
    client = ConsoleClient()

    async def authenticated():
        return client

    monkeypatch.setattr(app, "_ensure_authenticated", authenticated)
    assert (await app._test_connection())["connected"] is True
    fabric = await app._discover_fabric()
    assert fabric == {
        "success": True,
        "device_count": 2,
        "site_count": 2,
        "unreachable": 1,
    }
    alarms = await app._check_alarms()
    assert alarms == {"success": True, "count": 1, "critical": 1}

    async def unavailable():
        raise RuntimeError("offline")

    monkeypatch.setattr(app, "_ensure_authenticated", unavailable)
    assert (await app._test_connection())["connected"] is False
    assert (await app._discover_fabric())["success"] is False
    assert (await app._check_alarms())["success"] is False


def test_boot_sequence_connected(monkeypatch, capsys) -> None:
    results = iter([
        {"connected": True, "response_ms": 5},
        {"success": True, "device_count": 2, "site_count": 2, "unreachable": 1},
        {"success": True, "count": 1, "critical": 1},
    ])

    def fake_run(coroutine):
        coroutine.close()
        return next(results)

    monkeypatch.setenv("VMANAGE_HOST", "vmanage.example.test")
    monkeypatch.setenv("VMANAGE_USERNAME", "audit-user")
    monkeypatch.setattr(app, "_suppress_noisy_logs", lambda: None)
    monkeypatch.setattr(app, "_spin_print", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(app, "_run", fake_run)

    info = app._boot_sequence()

    output = capsys.readouterr().out
    assert info["connected"] is True
    assert info["device_count"] == 2
    assert "1 critical" in output
    assert "System ready" in output


def test_boot_sequence_without_host(monkeypatch, capsys) -> None:
    monkeypatch.delenv("VMANAGE_HOST", raising=False)
    monkeypatch.setattr(app, "_suppress_noisy_logs", lambda: None)
    monkeypatch.setattr(app, "_spin_print", lambda *_args, **_kwargs: None)

    assert app._boot_sequence() == {"connected": False}
    assert "No VMANAGE_HOST configured" in capsys.readouterr().out


def test_console_command_renderers(monkeypatch, capsys) -> None:
    client = ConsoleClient()
    monkeypatch.setattr(app, "_shared_client", client)
    monkeypatch.setattr(app, "_run", _run_coroutine)
    monkeypatch.setattr(app, "_spin_print", lambda *_args, **_kwargs: None)

    app._cmd_status()
    app._cmd_devices()
    app._cmd_health()
    app._cmd_alarms()
    app._cmd_diagnose("10.0.0.1")
    app._cmd_smoke_test()

    output = capsys.readouterr().out
    assert "vManage Connection" in output
    assert "edge-1" in output
    assert "Fabric Health:" in output
    assert "Root-Cause Hypotheses" in output
    assert "Device Diagnosis:" in output
    assert "Smoke Test:" in output


def test_console_empty_command_renderers(monkeypatch, capsys) -> None:
    client = ConsoleClient()

    async def empty_get(endpoint: str, params=None) -> dict:
        del endpoint, params
        return {"data": []}

    client.get = empty_get
    monkeypatch.setattr(app, "_shared_client", client)
    monkeypatch.setattr(app, "_run", _run_coroutine)

    app._cmd_devices()
    app._cmd_alarms()

    output = capsys.readouterr().out
    assert "No devices found" in output
    assert "No active alarms" in output


def test_shell_manual_commands(monkeypatch, capsys) -> None:
    responses = iter(["help", "clear", "status", "exit"])
    calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr("builtins.input", lambda _prompt: next(responses))
    monkeypatch.setattr(app, "_clear_screen", lambda: calls.append(("clear", [])))
    monkeypatch.setattr(
        app,
        "_run_cli_command",
        lambda command, arguments: calls.append((command, arguments)),
    )

    app._run_shell()

    assert ("clear", []) in calls
    assert ("status", []) in calls
    assert "Goodbye" in capsys.readouterr().out


def test_shell_ai_conversation(monkeypatch, capsys) -> None:
    responses = iter(["How is the fabric?", "exit"])
    provider = SimpleNamespace(chat=lambda _message: "line one\nline two")
    monkeypatch.setattr("builtins.input", lambda _prompt: next(responses))

    app._run_shell(ai_provider=provider)

    output = capsys.readouterr().out
    assert "line one" in output
    assert "line two" in output


def test_main_wires_runtime_and_closes_client(monkeypatch) -> None:
    from cisco_vmanage_mcp import ai_providers

    client = ConsoleClient()
    calls: dict[str, object] = {}
    monkeypatch.setattr(app, "_shared_client", client)
    monkeypatch.setattr(app, "_clear_screen", lambda: None)
    monkeypatch.setattr(app, "_boot_sequence", lambda: {"connected": True})
    monkeypatch.setattr(app, "_run_shell", lambda ai_provider=None: calls.update(shell=ai_provider))
    monkeypatch.setattr(ai_providers, "auto_connect", lambda: "provider")
    monkeypatch.setattr(ai_providers, "login_flow", lambda: None)
    monkeypatch.setattr(
        ai_providers,
        "set_app_runtime",
        lambda run_fn, client_fn: calls.update(runtime=(run_fn, client_fn)),
    )
    monkeypatch.setattr(app, "_get_loop", lambda: SimpleNamespace(run_until_complete=_run_coroutine))

    app.main()

    assert calls["shell"] == "provider"
    assert "runtime" in calls
    assert client.closed is True
