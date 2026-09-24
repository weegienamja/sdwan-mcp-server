"""Offline contracts for AI provider adapters and tool execution."""

from __future__ import annotations

import asyncio
import json
import sys
from types import ModuleType, SimpleNamespace

import pytest

from cisco_vmanage_mcp import ai_providers


class ProviderClient:
    def __init__(self, *, empty: bool = False, fail: bool = False) -> None:
        self.host = "vmanage.example.test"
        self.port = "443"
        self.empty = empty
        self.fail = fail

    async def get(self, endpoint: str, params=None) -> dict:
        if self.fail:
            raise RuntimeError("vManage unavailable")
        if self.empty:
            return {"data": []}
        devices = [
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
                "reachability": "reachable",
                "site-id": "100",
                "state": "green",
                "bfdSessions": 8,
                "controlConnections": 3,
            },
        ]
        responses = {
            "/dataservice/device": devices,
            "/dataservice/alarms": [
                {
                    "severity": "Critical",
                    "type": "Control",
                    "host_name": "edge-1",
                    "system_ip": "10.0.0.1",
                    "message": "x" * 150,
                }
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


def _run_coroutine(coroutine):
    return asyncio.run(coroutine)


def _install_runtime(monkeypatch, client: ProviderClient) -> None:
    async def get_client():
        return client

    monkeypatch.setattr(ai_providers, "_app_run", _run_coroutine)
    monkeypatch.setattr(ai_providers, "_app_client", get_client)


def test_runtime_defaults_fail_fast() -> None:
    async def sample():
        return "unused"

    with pytest.raises(RuntimeError, match="not been initialized"):
        ai_providers._runtime_unavailable(sample())
    with pytest.raises(RuntimeError, match="not been initialized"):
        asyncio.run(ai_providers._client_unavailable())


def test_set_app_runtime_replaces_callbacks(monkeypatch) -> None:
    monkeypatch.setattr(ai_providers, "_app_run", ai_providers._runtime_unavailable)
    monkeypatch.setattr(ai_providers, "_app_client", ai_providers._client_unavailable)

    async def client_getter():
        return "client"

    ai_providers.set_app_runtime(_run_coroutine, client_getter)

    async def value():
        return 7

    async def current_client():
        return await ai_providers._app_client()

    assert ai_providers._app_run(value()) == 7
    assert asyncio.run(current_client()) == "client"


def test_provider_tool_adapters(monkeypatch) -> None:
    _install_runtime(monkeypatch, ProviderClient())

    devices = json.loads(ai_providers._tool_devices())
    alarms = json.loads(ai_providers._tool_alarms())
    health = json.loads(ai_providers._tool_health())
    diagnosis = json.loads(ai_providers._tool_diagnose("10.0.0.1"))
    status = json.loads(ai_providers._tool_status())
    smoke = json.loads(ai_providers._tool_smoke())

    assert len(devices) == 2
    assert devices[1]["hostname"] == "edge-1"
    assert alarms[0]["device"] == "edge-1"
    assert len(alarms[0]["message"]) == 120
    assert health["overall_health"] == "healthy"
    assert health["impact"]["scope"] == "none"
    assert diagnosis["device"]["hostname"] == "edge-1"
    assert diagnosis["fabric_health"] == "healthy"
    assert status["connected"] is True
    assert status["total_devices"] == 2
    assert smoke["overall"] is True


def test_provider_tool_empty_and_failure_results(monkeypatch) -> None:
    _install_runtime(monkeypatch, ProviderClient(empty=True))
    assert ai_providers._tool_devices() == "No devices found."
    assert ai_providers._tool_alarms() == "No active alarms."
    assert ai_providers._tool_diagnose("") == "Error: system_ip is required."

    _install_runtime(monkeypatch, ProviderClient(fail=True))
    smoke = json.loads(ai_providers._tool_smoke())
    assert smoke["overall"] is False
    assert smoke["checks"][0]["check"] == "connection"


@pytest.mark.parametrize(
    ("name", "target"),
    [
        ("get_device_list", "_tool_devices"),
        ("get_alarms", "_tool_alarms"),
        ("get_fabric_health", "_tool_health"),
        ("diagnose_device", "_tool_diagnose"),
        ("get_status", "_tool_status"),
        ("run_smoke_test", "_tool_smoke"),
    ],
)
def test_exec_tool_dispatches(monkeypatch, name, target) -> None:
    calls: list[tuple] = []
    monkeypatch.setattr(
        ai_providers,
        target,
        lambda *args: calls.append(args) or "result",
    )

    assert ai_providers._exec_tool(name, {"system_ip": "10.0.0.1"}) == "result"
    assert len(calls) == 1


def test_exec_tool_handles_unknown_and_errors(monkeypatch) -> None:
    assert ai_providers._exec_tool("unknown", {}) == "Unknown tool: unknown"
    monkeypatch.setattr(
        ai_providers,
        "_tool_devices",
        lambda: (_ for _ in ()).throw(RuntimeError("failed")),
    )
    assert ai_providers._exec_tool("get_device_list", {}) == "Tool error: failed"


def test_base_provider_and_factory(monkeypatch) -> None:
    provider = ai_providers.AIProvider("key")
    with pytest.raises(NotImplementedError):
        provider.chat("hello")

    class FakeProvider(ai_providers.AIProvider):
        def chat(self, user_message: str) -> str:
            return user_message

    monkeypatch.setitem(ai_providers.PROVIDER_CLASSES, "fake", FakeProvider)
    assert isinstance(ai_providers.create_provider("fake", "key"), FakeProvider)
    with pytest.raises(ValueError, match="Unknown provider"):
        ai_providers.create_provider("missing", "key")


def test_provider_tool_schema_conversion() -> None:
    claude = ai_providers.ClaudeProvider.__new__(ai_providers.ClaudeProvider)
    openai = ai_providers.OpenAIProvider.__new__(ai_providers.OpenAIProvider)

    claude_tools = claude._convert_tools()
    openai_tools = openai._convert_tools()

    assert len(claude_tools) == len(ai_providers.TOOL_DEFINITIONS)
    assert claude_tools[0]["input_schema"]["type"] == "object"
    assert openai_tools[0]["type"] == "function"
    assert openai_tools[0]["function"]["name"] == "get_device_list"


def test_claude_chat_executes_tool_then_returns_text(monkeypatch) -> None:
    tool_block = SimpleNamespace(
        type="tool_use",
        name="get_status",
        input={},
        id="call-1",
    )
    text_block = SimpleNamespace(type="text", text="Fabric is healthy")
    responses = iter([
        SimpleNamespace(stop_reason="tool_use", content=[tool_block]),
        SimpleNamespace(stop_reason="end_turn", content=[text_block]),
    ])
    provider = ai_providers.ClaudeProvider.__new__(ai_providers.ClaudeProvider)
    provider.client = SimpleNamespace(
        messages=SimpleNamespace(create=lambda **_kwargs: next(responses))
    )
    provider.model = "test"
    provider._tools = []
    provider.conversation = []
    monkeypatch.setattr(ai_providers, "_exec_tool", lambda _name, _args: "{}")

    assert provider.chat("status") == "Fabric is healthy"
    assert provider.conversation[-1]["role"] == "assistant"


def test_openai_chat_executes_tool_then_returns_text(monkeypatch) -> None:
    tool_call = SimpleNamespace(
        id="call-1",
        function=SimpleNamespace(name="get_status", arguments="{}"),
    )
    responses = iter([
        SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=[tool_call], content=None))]
        ),
        SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=None, content="Healthy"))]
        ),
    ])
    provider = ai_providers.OpenAIProvider.__new__(ai_providers.OpenAIProvider)
    provider.client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=lambda **_kwargs: next(responses))
        )
    )
    provider.model = "test"
    provider._tools = []
    provider.messages = []
    monkeypatch.setattr(ai_providers, "_exec_tool", lambda _name, _args: "{}")

    assert provider.chat("status") == "Healthy"
    assert any(message.get("role") == "tool" for message in provider.messages if isinstance(message, dict))


def _fake_google_types(monkeypatch):
    class Value:
        def __init__(self, **values) -> None:
            self.__dict__.update(values)

    class Part:
        @staticmethod
        def from_function_response(**values):
            return Value(**values)

    fake_types = SimpleNamespace(
        Type=SimpleNamespace(STRING="string", OBJECT="object"),
        Schema=Value,
        FunctionDeclaration=Value,
        Tool=Value,
        GenerateContentConfig=Value,
        Part=Part,
    )
    module = ModuleType("google.genai")
    module.__dict__["types"] = fake_types
    monkeypatch.setitem(sys.modules, "google.genai", module)
    return fake_types


def test_gemini_schema_and_chat_tool_cycle(monkeypatch) -> None:
    _fake_google_types(monkeypatch)
    provider = ai_providers.GeminiProvider.__new__(ai_providers.GeminiProvider)
    provider.model = "test"
    provider._tools = provider._convert_tools()

    function_call = SimpleNamespace(name="get_status", args={})
    call_part = SimpleNamespace(function_call=function_call, text=None)
    text_part = SimpleNamespace(function_call=None, text="Healthy")
    responses = iter([
        SimpleNamespace(
            candidates=[SimpleNamespace(content=SimpleNamespace(parts=[call_part]))]
        ),
        SimpleNamespace(
            candidates=[SimpleNamespace(content=SimpleNamespace(parts=[text_part]))]
        ),
    ])

    class Chat:
        def send_message(self, _message):
            return next(responses)

    chat = Chat()
    provider.genai_client = SimpleNamespace(
        chats=SimpleNamespace(create=lambda **_kwargs: chat)
    )
    provider._chat = None
    monkeypatch.setattr(ai_providers, "_exec_tool", lambda _name, _args: "{}")

    assert provider.chat("status") == "Healthy"
    assert provider._chat is chat


def test_keyring_helpers_success_and_failure(monkeypatch) -> None:
    calls: list[tuple] = []
    keyring = SimpleNamespace(
        get_password=lambda service, provider: calls.append(("get", service, provider)) or "key",
        set_password=lambda service, provider, key: calls.append(("set", service, provider, key)),
        delete_password=lambda service, provider: calls.append(("delete", service, provider)),
    )
    monkeypatch.setattr(ai_providers, "import_module", lambda _name: keyring)

    assert ai_providers._get_saved_api_key("openai") == "key"
    assert ai_providers._store_saved_api_key("openai", "secret") is True
    ai_providers._delete_saved_api_key("openai")
    assert [call[0] for call in calls] == ["get", "set", "delete"]

    monkeypatch.setattr(
        ai_providers,
        "import_module",
        lambda _name: (_ for _ in ()).throw(RuntimeError("unavailable")),
    )
    assert ai_providers._get_saved_api_key("openai") is None
    assert ai_providers._store_saved_api_key("openai", "secret") is False
    ai_providers._delete_saved_api_key("openai")


def test_load_config_migrates_legacy_key(monkeypatch, tmp_path) -> None:
    config_dir = tmp_path / "config"
    config_file = config_dir / "config.json"
    config_dir.mkdir()
    config_file.write_text(
        json.dumps({"provider": "openai", "api_key": "legacy-key"}),
        encoding="utf-8",
    )
    stored: list[tuple[str, str]] = []
    monkeypatch.setattr(ai_providers, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(ai_providers, "CONFIG_FILE", config_file)
    monkeypatch.setattr(
        ai_providers,
        "_store_saved_api_key",
        lambda provider, key: stored.append((provider, key)) or True,
    )

    assert ai_providers._load_config() == {"provider": "openai"}
    assert stored == [("openai", "legacy-key")]
    assert "api_key" not in config_file.read_text(encoding="utf-8")

    config_file.write_text("invalid json", encoding="utf-8")
    assert ai_providers._load_config() == {}


def test_validate_api_key_classifies_results(monkeypatch) -> None:
    monkeypatch.setattr(
        ai_providers,
        "create_provider",
        lambda _name, _key: SimpleNamespace(chat=lambda _message: "OK"),
    )
    assert ai_providers.validate_api_key("openai", "key") == (True, "API key validated")

    monkeypatch.setattr(
        ai_providers,
        "create_provider",
        lambda _name, _key: SimpleNamespace(chat=lambda _message: "x" * 250),
    )
    assert ai_providers.validate_api_key("openai", "key") == (True, "Connected")

    for message, expected in [
        ("invalid api key", "Invalid API key"),
        ("module missing", "Missing library: pip install openai"),
        ("network unavailable", "Connection error: network unavailable"),
    ]:
        monkeypatch.setattr(
            ai_providers,
            "create_provider",
            lambda _name, _key, error=message: (_ for _ in ()).throw(RuntimeError(error)),
        )
        assert ai_providers.validate_api_key("openai", "key")[1] == expected

    assert ai_providers._get_pip_package("unknown") == ""


def test_auto_connect_uses_saved_provider(monkeypatch) -> None:
    provider = object()
    monkeypatch.setattr(ai_providers, "_load_config", lambda: {"provider": "openai"})
    monkeypatch.setattr(ai_providers, "_get_saved_api_key", lambda _name: "saved-key")
    monkeypatch.setattr(ai_providers, "_check_library", lambda _name: True)
    monkeypatch.setattr(ai_providers, "create_provider", lambda _name, _key: provider)

    assert ai_providers.auto_connect() is provider


def test_auto_connect_multiple_provider_selection(monkeypatch) -> None:
    provider = object()
    saved: list[dict] = []
    monkeypatch.setattr(ai_providers, "_load_config", lambda: {})
    monkeypatch.setattr(ai_providers, "_check_library", lambda _name: True)
    monkeypatch.setattr(ai_providers, "create_provider", lambda _name, _key: provider)
    monkeypatch.setattr(ai_providers, "_save_config", lambda config: saved.append(dict(config)))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-secret-value")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret-value")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setattr("builtins.input", lambda _prompt: "2")

    assert ai_providers.auto_connect() is provider
    assert saved == [{"provider": "openai"}]


def test_auto_connect_cancel_and_invalid_selection(monkeypatch) -> None:
    monkeypatch.setattr(ai_providers, "_load_config", lambda: {})
    monkeypatch.setattr(ai_providers, "_check_library", lambda _name: True)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-secret-value")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret-value")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    monkeypatch.setattr("builtins.input", lambda _prompt: "0")
    assert ai_providers.auto_connect() is None
    monkeypatch.setattr("builtins.input", lambda _prompt: "invalid")
    assert ai_providers.auto_connect() is None


def test_login_flow_uses_saved_provider(monkeypatch) -> None:
    provider = object()
    monkeypatch.setattr(ai_providers, "_load_config", lambda: {"provider": "openai"})
    monkeypatch.setattr(ai_providers, "_get_saved_api_key", lambda _name: "saved-key")
    monkeypatch.setattr(ai_providers, "_check_library", lambda _name: True)
    monkeypatch.setattr(ai_providers, "create_provider", lambda _name, _key: provider)
    monkeypatch.setattr("builtins.input", lambda _prompt: "")

    assert ai_providers.login_flow() is provider


def test_login_flow_with_environment_key_and_keyring(monkeypatch) -> None:
    provider = object()
    answers = iter(["2", "", ""])
    saved: list[dict] = []
    stored: list[tuple[str, str]] = []
    monkeypatch.setattr(ai_providers, "_load_config", lambda: {})
    monkeypatch.setattr(ai_providers, "_check_library", lambda _name: True)
    monkeypatch.setattr(ai_providers, "validate_api_key", lambda _name, _key: (True, "valid"))
    monkeypatch.setattr(ai_providers, "create_provider", lambda _name, _key: provider)
    monkeypatch.setattr(ai_providers, "_save_config", lambda config: saved.append(dict(config)))
    monkeypatch.setattr(
        ai_providers,
        "_store_saved_api_key",
        lambda name, key: stored.append((name, key)) or True,
    )
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret-value")
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    assert ai_providers.login_flow() is provider
    assert saved == [{"provider": "openai"}]
    assert stored == [("openai", "openai-secret-value")]


def test_login_flow_cancel_and_missing_library(monkeypatch) -> None:
    monkeypatch.setattr(ai_providers, "_load_config", lambda: {})
    monkeypatch.setattr(ai_providers, "_check_library", lambda _name: False)
    monkeypatch.setattr("builtins.input", lambda _prompt: "0")
    assert ai_providers.login_flow() is None

    monkeypatch.setattr("builtins.input", lambda _prompt: "1")
    assert ai_providers.login_flow() is None
