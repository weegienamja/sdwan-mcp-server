"""Security and reliability tests for structured audit logging."""

from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler

import pytest

from cisco_vmanage_mcp.services import audit


@pytest.fixture
def isolated_audit_logger():
    logger = logging.getLogger("cisco_vmanage_mcp.audit")
    original_handlers = logger.handlers[:]
    logger.handlers.clear()
    try:
        yield logger
    finally:
        for handler in logger.handlers:
            handler.close()
        logger.handlers[:] = original_handlers


def test_redact_matches_common_secret_key_variants() -> None:
    redacted = audit._redact(
        {
            "auth_token": "token-value",
            "clientSecret": "secret-value",
            "Authorization": "Bearer value",
            "safe": "visible",
        }
    )

    assert redacted["auth_token"] == audit._REDACT_PLACEHOLDER
    assert redacted["clientSecret"] == audit._REDACT_PLACEHOLDER
    assert redacted["Authorization"] == audit._REDACT_PLACEHOLDER
    assert redacted["safe"] == "visible"


@pytest.mark.asyncio
async def test_audit_decorator_does_not_log_result_content(monkeypatch) -> None:
    captured: dict = {}

    def capture_log(_tool_name, _parameters, **fields) -> None:
        captured.update(fields)

    monkeypatch.setattr(audit, "log_tool_call", capture_log)

    @audit.audit_tool("sample")
    async def sample_tool() -> str:
        return "device=branch-edge-1 system_ip=10.0.0.1"

    result = await sample_tool()

    assert result.startswith("device=branch-edge-1")
    assert captured["result_summary"] == f"str result ({len(result)} chars)"
    assert "branch-edge-1" not in captured["result_summary"]


def test_audit_file_is_private_and_rotating(
    isolated_audit_logger,
    monkeypatch,
    tmp_path,
) -> None:
    log_path = tmp_path / "logs" / "audit.jsonl"
    monkeypatch.setenv("AUDIT_LOG_PATH", str(log_path))
    monkeypatch.delenv("AUDIT_STDERR", raising=False)

    logger = audit._setup_audit_logger()

    assert any(isinstance(handler, RotatingFileHandler) for handler in logger.handlers)
    assert not any(type(handler) is logging.StreamHandler for handler in logger.handlers)
    assert log_path.stat().st_mode & 0o777 == 0o600
    assert log_path.parent.stat().st_mode & 0o777 == 0o700


def test_audit_setup_failure_is_nonfatal_and_visible(
    isolated_audit_logger,
    monkeypatch,
    tmp_path,
) -> None:
    invalid_parent = tmp_path / "not-a-directory"
    invalid_parent.write_text("file", encoding="utf-8")
    monkeypatch.setenv("AUDIT_LOG_PATH", str(invalid_parent / "audit.jsonl"))
    monkeypatch.delenv("AUDIT_STDERR", raising=False)

    with pytest.warns(RuntimeWarning, match="Audit file logging is unavailable"):
        logger = audit._setup_audit_logger()

    assert logger is isolated_audit_logger


def test_browser_audit_excludes_headers_queries_and_bodies(monkeypatch) -> None:
    entries: list[str] = []
    monkeypatch.setattr(audit._audit_logger, "info", entries.append)

    audit.log_browser_request(
        "POST",
        "/api/v1/investigations/{investigation_id}",
        201,
        "request-123",
        actor_id="oidc-user",
        tenant_id="customer-a",
        role="operator",
        duration_ms=12.34,
    )

    payload = json.loads(entries[0])
    assert payload["event"] == "browser_request"
    assert payload["route"].endswith("{investigation_id}")
    assert payload["actor_id"] == "oidc-user"
    assert payload["duration_ms"] == 12.3
    assert "headers" not in payload
    assert "query" not in payload
    assert "body" not in payload
