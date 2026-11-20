"""Credential-storage tests for optional AI providers."""

from __future__ import annotations

import ssl
from types import ModuleType, SimpleNamespace

from cisco_vmanage_mcp import ai_providers
from tests.permissions import assert_private_permissions


def _use_temp_config(monkeypatch, tmp_path) -> None:
    config_dir = tmp_path / ".vmanage-mcp"
    monkeypatch.setattr(ai_providers, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(ai_providers, "CONFIG_FILE", config_dir / "config.json")


def test_config_never_persists_raw_api_key(monkeypatch, tmp_path) -> None:
    _use_temp_config(monkeypatch, tmp_path)

    ai_providers._save_config({"provider": "openai", "api_key": "sk-secret"})

    contents = ai_providers.CONFIG_FILE.read_text(encoding="utf-8")
    assert "sk-secret" not in contents
    assert "api_key" not in contents
    assert_private_permissions(ai_providers.CONFIG_FILE, 0o600)
    assert_private_permissions(ai_providers.CONFIG_DIR, 0o700)


def test_auto_connect_does_not_copy_environment_key_to_config(
    monkeypatch,
    tmp_path,
) -> None:
    _use_temp_config(monkeypatch, tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-environment")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setattr(ai_providers, "_check_library", lambda name: name == "openai")
    monkeypatch.setattr(
        ai_providers,
        "create_provider",
        lambda _provider, _key: object(),
    )

    provider = ai_providers.auto_connect()

    assert provider is not None
    contents = ai_providers.CONFIG_FILE.read_text(encoding="utf-8")
    assert "sk-from-environment" not in contents
    assert "api_key" not in contents


def test_enterprise_ca_bundle_requires_explicit_opt_in(monkeypatch) -> None:
    monkeypatch.delenv("AI_CA_BUNDLE", raising=False)

    assert ai_providers.provider_ca_bundle_path() is None

    monkeypatch.setenv("AI_CA_BUNDLE", "bundled")
    bundle = ai_providers.provider_ca_bundle_path()
    assert bundle is not None
    assert ai_providers.Path(bundle).is_file()


def test_custom_provider_ca_verifies_certificates_and_hostnames(monkeypatch) -> None:
    sdk = ModuleType("test_provider_sdk")
    monkeypatch.setattr(sdk, "DefaultHttpxClient", lambda *, verify: verify, raising=False)
    monkeypatch.setenv("AI_CA_BUNDLE", "bundled")

    context = ai_providers._provider_transport(sdk)

    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname
    assert context.get_ca_certs()


def test_claude_tool_loop_is_bounded(monkeypatch) -> None:
    calls = 0
    block = SimpleNamespace(type="tool_use", name="get_status", input={}, id="call-1")
    response = SimpleNamespace(stop_reason="tool_use", content=[block])

    def create(**_kwargs):
        nonlocal calls
        calls += 1
        if calls > 3:
            raise AssertionError("Claude tool loop did not stop")
        return response

    provider = ai_providers.ClaudeProvider.__new__(ai_providers.ClaudeProvider)
    provider.client = SimpleNamespace(messages=SimpleNamespace(create=create))
    provider.model = "test"
    provider._tools = []
    provider.conversation = []
    monkeypatch.setattr(ai_providers, "MAX_TOOL_ROUNDS", 2, raising=False)
    monkeypatch.setattr(ai_providers, "_exec_tool", lambda _name, _args: "{}")

    result = provider.chat("status")

    assert result == "Tool call limit reached. Start a new request to continue."
    assert calls == 2


def test_openai_tool_loop_is_bounded(monkeypatch) -> None:
    calls = 0
    tool_call = SimpleNamespace(
        id="call-1",
        function=SimpleNamespace(name="get_status", arguments="{}"),
    )
    message = SimpleNamespace(tool_calls=[tool_call], content=None)
    response = SimpleNamespace(choices=[SimpleNamespace(message=message)])

    def create(**_kwargs):
        nonlocal calls
        calls += 1
        if calls > 3:
            raise AssertionError("OpenAI tool loop did not stop")
        return response

    provider = ai_providers.OpenAIProvider.__new__(ai_providers.OpenAIProvider)
    provider.client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    provider.model = "test"
    provider._tools = []
    provider.messages = []
    monkeypatch.setattr(ai_providers, "MAX_TOOL_ROUNDS", 2, raising=False)
    monkeypatch.setattr(ai_providers, "_exec_tool", lambda _name, _args: "{}")

    result = provider.chat("status")

    assert result == "Tool call limit reached. Start a new request to continue."
    assert calls == 2
