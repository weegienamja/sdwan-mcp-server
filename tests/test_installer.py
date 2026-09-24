"""Security tests for MCP client installation."""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from cisco_vmanage_mcp import installer

CREDS = {
    "host": "vmanage.example.test",
    "port": "443",
    "username": "audit-user",
    "password": "super-secret-password",
    "verify_ssl": "true",
}


def test_server_config_references_credential_file_without_secrets(
    monkeypatch,
    tmp_path,
) -> None:
    credentials_file = tmp_path / "vmanage.env"
    monkeypatch.setattr(installer, "CREDENTIALS_FILE", credentials_file, raising=False)

    config = installer._build_server_config(**CREDS)
    serialized = json.dumps(config)

    assert CREDS["password"] not in serialized
    assert CREDS["username"] not in serialized
    assert config["env"]["VMANAGE_CONFIG_FILE"] == str(credentials_file)


def test_credentials_file_is_owner_only(monkeypatch, tmp_path) -> None:
    credentials_file = tmp_path / "config" / "vmanage.env"
    monkeypatch.setattr(installer, "CREDENTIALS_FILE", credentials_file, raising=False)

    installer._write_credentials_file(CREDS, dry_run=False)

    contents = credentials_file.read_text(encoding="utf-8")
    assert "VMANAGE_PASSWORD='super-secret-password'" in contents
    assert credentials_file.stat().st_mode & 0o777 == 0o600
    assert credentials_file.parent.stat().st_mode & 0o777 == 0o700


def test_installer_dry_run_never_prints_password(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        installer,
        "CREDENTIALS_FILE",
        installer.Path.home() / ".vmanage-mcp" / "vmanage.env",
        raising=False,
    )

    installer._write_credentials_file(CREDS, dry_run=True)
    installer._configure_claude_code(CREDS, dry_run=True)

    assert CREDS["password"] not in capsys.readouterr().out


def test_claude_code_command_contains_no_credentials(monkeypatch, tmp_path) -> None:
    credentials_file = tmp_path / "vmanage.env"
    monkeypatch.setattr(installer, "CREDENTIALS_FILE", credentials_file, raising=False)
    captured: dict = {}

    def fake_run(command, **_kwargs):
        captured["command"] = command
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(installer.subprocess, "run", fake_run)

    installer._configure_claude_code(CREDS, dry_run=False)

    command = " ".join(captured["command"])
    assert CREDS["password"] not in command
    assert CREDS["username"] not in command
    assert f"VMANAGE_CONFIG_FILE={credentials_file}" in command


def test_written_client_config_is_owner_only(tmp_path) -> None:
    config_path = tmp_path / "client" / "config.json"

    installer._write_json_config(
        config_path,
        {"command": "python", "args": ["-m", "cisco_vmanage_mcp"]},
        dry_run=False,
    )

    assert config_path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("system", ["Darwin", "Linux", "Windows"])
def test_client_config_paths_for_supported_platforms(
    monkeypatch,
    tmp_path,
    system,
) -> None:
    monkeypatch.setattr(installer.platform, "system", lambda: system)
    monkeypatch.setattr(installer.Path, "home", lambda: tmp_path)
    if system == "Linux":
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    if system == "Windows":
        appdata = tmp_path / "AppData"
        appdata.mkdir()
        monkeypatch.setenv("APPDATA", str(appdata))

    configs = installer._get_client_configs()

    assert set(configs) == {"Claude Desktop", "Cursor"}
    assert all(path.name.endswith(".json") for path in configs.values())


def test_detect_claude_code(monkeypatch) -> None:
    monkeypatch.setattr(installer.shutil, "which", lambda _name: "/usr/bin/claude")
    assert installer._detect_claude_code() is True

    monkeypatch.setattr(installer.shutil, "which", lambda _name: None)
    assert installer._detect_claude_code() is False


def test_merge_config_preserves_existing_servers() -> None:
    existing = {
        "theme": "dark",
        "mcpServers": {"other": {"command": "other-server"}},
    }
    server = {"command": "python"}

    merged = installer._merge_config(existing, server)

    assert merged["theme"] == "dark"
    assert merged["mcpServers"]["other"]["command"] == "other-server"
    assert merged["mcpServers"]["cisco-vmanage"] == server


def test_prompt_credentials_uses_defaults_and_enables_tls(monkeypatch) -> None:
    answers = iter(["", "", "audit-user", "yes"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    monkeypatch.setattr(installer.getpass, "getpass", lambda _prompt: "secret")

    credentials = installer._prompt_credentials()

    assert credentials == {
        "host": "sandbox-sdwan-2.cisco.com",
        "port": "443",
        "username": "audit-user",
        "password": "secret",
        "verify_ssl": "true",
    }


@pytest.mark.parametrize("missing", ["username", "password"])
def test_prompt_credentials_rejects_missing_values(monkeypatch, missing) -> None:
    username = "" if missing == "username" else "audit-user"
    answers = iter(["vmanage.example.test", "443", username])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    monkeypatch.setattr(
        installer.getpass,
        "getpass",
        lambda _prompt: "" if missing == "password" else "secret",
    )

    with pytest.raises(SystemExit) as exc_info:
        installer._prompt_credentials()

    assert exc_info.value.code == 1


def test_prompt_client_selection_supports_json_cli_and_manual(
    monkeypatch,
    tmp_path,
) -> None:
    available = {
        "Claude Desktop": tmp_path / "claude.json",
        "Cursor": tmp_path / "cursor.json",
    }
    monkeypatch.setattr("builtins.input", lambda _prompt: "1,3,4")

    clients, configure_claude = installer._prompt_client_selection(
        available,
        claude_code=True,
    )

    assert clients == ["Claude Desktop", "__manual__"]
    assert configure_claude is True


def test_prompt_client_selection_handles_none_and_invalid(
    monkeypatch,
) -> None:
    assert installer._prompt_client_selection({}, claude_code=False) == ([], False)

    monkeypatch.setattr("builtins.input", lambda _prompt: "invalid")
    with pytest.raises(SystemExit) as exc_info:
        installer._prompt_client_selection(
            {"Claude Desktop": installer.Path("missing.json")},
            claude_code=False,
        )
    assert exc_info.value.code == 1


def test_dotenv_quote_escapes_values_and_rejects_newlines() -> None:
    assert installer._dotenv_quote("a'b\\c") == "'a\\'b\\\\c'"
    with pytest.raises(ValueError, match="newlines"):
        installer._dotenv_quote("first\nsecond")


def test_json_config_merges_existing_and_recovers_invalid(tmp_path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"mcpServers": {"other": {"command": "other"}}}),
        encoding="utf-8",
    )

    installer._write_json_config(config_path, {"command": "python"}, dry_run=False)
    merged = json.loads(config_path.read_text(encoding="utf-8"))
    assert set(merged["mcpServers"]) == {"other", "cisco-vmanage"}

    config_path.write_text("not json", encoding="utf-8")
    installer._write_json_config(config_path, {"command": "python"}, dry_run=False)
    recovered = json.loads(config_path.read_text(encoding="utf-8"))
    assert recovered["mcpServers"]["cisco-vmanage"]["command"] == "python"


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (SimpleNamespace(returncode=1, stdout="", stderr="failed"), "configuration failed"),
        (FileNotFoundError(), "CLI not found"),
        (subprocess.TimeoutExpired("claude", 30), "timed out"),
    ],
)
def test_claude_code_failures_are_reported(
    monkeypatch,
    capsys,
    failure,
    expected,
) -> None:
    def fake_run(*_args, **_kwargs):
        if isinstance(failure, BaseException):
            raise failure
        return failure

    monkeypatch.setattr(installer.subprocess, "run", fake_run)

    installer._configure_claude_code(CREDS, dry_run=False)

    assert expected in capsys.readouterr().out


def test_main_dry_run_orchestrates_selected_clients(monkeypatch, tmp_path) -> None:
    client_path = tmp_path / "client.json"
    calls: list[tuple] = []
    monkeypatch.setattr(installer, "_validate_environment", lambda: [])
    monkeypatch.setattr(
        installer,
        "_get_client_configs",
        lambda: {"Claude Desktop": client_path},
    )
    monkeypatch.setattr(installer, "_detect_claude_code", lambda: True)
    monkeypatch.setattr(
        installer,
        "_prompt_client_selection",
        lambda _available, _claude: (["Claude Desktop", "__manual__"], True),
    )
    monkeypatch.setattr(installer, "_prompt_credentials", lambda: dict(CREDS))
    monkeypatch.setattr(
        installer,
        "_write_credentials_file",
        lambda creds, dry_run: calls.append(("credentials", dry_run, creds["host"])),
    )
    monkeypatch.setattr(
        installer,
        "_write_json_config",
        lambda path, _config, dry_run: calls.append(("json", path, dry_run)),
    )
    monkeypatch.setattr(
        installer,
        "_print_manual_config",
        lambda _config: calls.append(("manual",)),
    )
    monkeypatch.setattr(
        installer,
        "_configure_claude_code",
        lambda _creds, dry_run: calls.append(("claude", dry_run)),
    )

    installer.main(["--dry-run"])

    assert ("credentials", True, CREDS["host"]) in calls
    assert ("json", client_path, True) in calls
    assert ("manual",) in calls
    assert ("claude", True) in calls
