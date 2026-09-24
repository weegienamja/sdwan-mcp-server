"""Audit logging for every tool call and API interaction.

Logs every prompt, tool invocation, parameters, API calls, and response
summaries. This provides operational auditability and is essential for
trust in a system where an LLM calls network infrastructure APIs.

Logs are structured JSON, written to a rotating file and optionally to stderr.
Credentials and sensitive fields are automatically redacted.
"""

from __future__ import annotations

import inspect
import json
import logging
import os
import re
import time
import warnings
from collections.abc import Callable
from functools import wraps
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, cast

# Sensitive field names to redact from logs
_REDACT_FIELDS = frozenset({
    "password", "j_password", "secret", "token", "api_key",
    "VMANAGE_PASSWORD", "VMANAGE_USERNAME",
    "X-XSRF-TOKEN", "JSESSIONID", "cookie",
})

_REDACT_PLACEHOLDER = "***REDACTED***"
_NORMALIZED_REDACT_FIELDS = frozenset(
    re.sub(r"[^a-z0-9]", "", field.lower())
    for field in _REDACT_FIELDS
)
_SENSITIVE_KEY_PARTS = (
    "apikey",
    "authorization",
    "cookie",
    "jsessionid",
    "password",
    "passwd",
    "secret",
    "sessionid",
    "token",
)


def _is_sensitive_key(key: Any) -> bool:
    """Return whether a mapping key is likely to identify secret material."""
    normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
    return normalized in _NORMALIZED_REDACT_FIELDS or any(
        part in normalized for part in _SENSITIVE_KEY_PARTS
    )


def _redact(obj: Any, depth: int = 0) -> Any:
    """Recursively redact sensitive fields from dicts/lists for logging."""
    if depth > 10:
        return "..."
    if isinstance(obj, dict):
        return {
            k: (_REDACT_PLACEHOLDER if _is_sensitive_key(k) else _redact(v, depth + 1))
            for k, v in obj.items()
        }
    if isinstance(obj, (list, tuple)):
        return [_redact(item, depth + 1) for item in obj]
    if isinstance(obj, str) and len(obj) > 500:
        return obj[:200] + f"...[truncated, {len(obj)} chars total]"
    return obj


def _setup_audit_logger() -> logging.Logger:
    """Configure the audit logger with structured JSON output."""
    logger = logging.getLogger("cisco_vmanage_mcp.audit")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    if any(
        getattr(handler, "_vmanage_audit_handler", False)
        for handler in logger.handlers
    ):
        return logger

    # File handler (if AUDIT_LOG_PATH is set)
    log_path = os.getenv("AUDIT_LOG_PATH")
    if log_path:
        try:
            path = Path(log_path).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            file_handler = RotatingFileHandler(
                path,
                maxBytes=int(os.getenv("AUDIT_LOG_MAX_BYTES", str(10 * 1024 * 1024))),
                backupCount=int(os.getenv("AUDIT_LOG_BACKUP_COUNT", "5")),
                encoding="utf-8",
            )
            path.chmod(0o600)
            file_handler.setLevel(logging.INFO)
            file_handler.setFormatter(logging.Formatter("%(message)s"))
            cast(Any, file_handler)._vmanage_audit_handler = True
            logger.addHandler(file_handler)
        except (OSError, ValueError) as exc:
            warnings.warn(
                f"Audit file logging is unavailable: {exc}",
                RuntimeWarning,
                stacklevel=2,
            )

    if os.getenv("AUDIT_STDERR", "false").lower() == "true":
        stderr_handler = logging.StreamHandler()
        stderr_handler.setLevel(logging.INFO)
        stderr_handler.setFormatter(logging.Formatter("[AUDIT] %(message)s"))
        cast(Any, stderr_handler)._vmanage_audit_handler = True
        logger.addHandler(stderr_handler)

    if not logger.handlers:
        null_handler = logging.NullHandler()
        cast(Any, null_handler)._vmanage_audit_handler = True
        logger.addHandler(null_handler)

    return logger


_audit_logger = _setup_audit_logger()


def log_tool_call(
    tool_name: str,
    parameters: dict[str, Any],
    result_summary: str | None = None,
    error: str | None = None,
    duration_ms: float | None = None,
) -> None:
    """Log a tool invocation with parameters and result summary."""
    entry = {
        "event": "tool_call",
        "timestamp": time.time(),
        "tool": tool_name,
        "parameters": _redact(parameters),
    }
    if result_summary is not None:
        entry["result_summary"] = result_summary[:500] if result_summary else ""
    if error is not None:
        entry["error"] = error
    if duration_ms is not None:
        entry["duration_ms"] = round(duration_ms, 1)

    _audit_logger.info(json.dumps(entry, default=str))


def log_api_call(
    method: str,
    endpoint: str,
    status_code: int | None = None,
    error: str | None = None,
    duration_ms: float | None = None,
) -> None:
    """Log a vManage API call."""
    entry = {
        "event": "api_call",
        "timestamp": time.time(),
        "method": method,
        "endpoint": endpoint,
    }
    if status_code is not None:
        entry["status_code"] = status_code
    if error is not None:
        entry["error"] = error
    if duration_ms is not None:
        entry["duration_ms"] = round(duration_ms, 1)

    _audit_logger.info(json.dumps(entry, default=str))


def log_browser_request(
    method: str,
    route: str,
    status_code: int,
    request_id: str,
    *,
    actor_id: str | None = None,
    tenant_id: str | None = None,
    role: str | None = None,
    duration_ms: float | None = None,
    error: str | None = None,
) -> None:
    """Log browser request metadata without headers, query values, or bodies."""
    entry = {
        "event": "browser_request",
        "timestamp": time.time(),
        "method": method,
        "route": route[:300],
        "status_code": status_code,
        "request_id": request_id,
    }
    if actor_id is not None:
        entry["actor_id"] = actor_id
    if tenant_id is not None:
        entry["tenant_id"] = tenant_id
    if role is not None:
        entry["role"] = role
    if duration_ms is not None:
        entry["duration_ms"] = round(duration_ms, 1)
    if error is not None:
        entry["error"] = error
    _audit_logger.info(json.dumps(entry, default=str))


def audit_tool(tool_name: str) -> Callable:
    """Decorator that wraps an MCP tool function with audit logging."""
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args, **kwargs):
            bound = inspect.signature(func).bind_partial(*args, **kwargs)
            logged_params = {
                key: value
                for key, value in bound.arguments.items()
                if key != "ctx"
            }
            start = time.monotonic()
            success = False
            try:
                result = await func(*args, **kwargs)
                duration = (time.monotonic() - start) * 1000
                success = not (
                    isinstance(result, str)
                    and result.startswith("Error:")
                )
                if isinstance(result, (str, bytes)):
                    summary = f"{type(result).__name__} result ({len(result)} chars)"
                elif hasattr(result, "__len__"):
                    summary = f"{type(result).__name__} result ({len(result)} items)"
                else:
                    summary = f"{type(result).__name__} result"
                if success:
                    log_tool_call(
                        tool_name,
                        logged_params,
                        result_summary=summary,
                        duration_ms=duration,
                    )
                else:
                    log_tool_call(
                        tool_name,
                        logged_params,
                        error="tool_error",
                        duration_ms=duration,
                    )
                return result
            except Exception as e:
                duration = (time.monotonic() - start) * 1000
                log_tool_call(tool_name, logged_params, error=type(e).__name__, duration_ms=duration)
                raise
            finally:
                from cisco_vmanage_mcp.telemetry import (
                    emit_ide_tool_event,
                    emit_tool_event,
                )

                duration = (time.monotonic() - start) * 1000
                emit_tool_event(
                    tool_name,
                    duration,
                    success,
                )
                await emit_ide_tool_event(tool_name, duration, success)
        cast(Any, wrapper).__audit_tool_name__ = tool_name
        return wrapper
    return decorator
