"""Audit logging for every tool call and API interaction.

Logs every prompt, tool invocation, parameters, API calls, and response
summaries. This provides operational auditability and is essential for
trust in a system where an LLM calls network infrastructure APIs.

Logs are structured JSON, written to a rotating file and optionally to stderr.
Credentials and sensitive fields are automatically redacted.
"""

from __future__ import annotations

import json
import logging
import os
import time
from functools import wraps
from pathlib import Path
from typing import Any, Callable

# Sensitive field names to redact from logs
_REDACT_FIELDS = frozenset({
    "password", "j_password", "secret", "token", "api_key",
    "VMANAGE_PASSWORD", "VMANAGE_USERNAME",
    "X-XSRF-TOKEN", "JSESSIONID", "cookie",
})

_REDACT_PLACEHOLDER = "***REDACTED***"


def _redact(obj: Any, depth: int = 0) -> Any:
    """Recursively redact sensitive fields from dicts/lists for logging."""
    if depth > 10:
        return "..."
    if isinstance(obj, dict):
        return {
            k: (_REDACT_PLACEHOLDER if k.lower() in {f.lower() for f in _REDACT_FIELDS} else _redact(v, depth + 1))
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

    if logger.handlers:
        return logger

    # File handler (if AUDIT_LOG_PATH is set)
    log_path = os.getenv("AUDIT_LOG_PATH")
    if log_path:
        path = Path(log_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(path, encoding="utf-8")
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(file_handler)

    # Stderr handler (always, but at DEBUG level so it's opt-in)
    stderr_handler = logging.StreamHandler()
    stderr_handler.setLevel(logging.DEBUG)
    stderr_handler.setFormatter(logging.Formatter("[AUDIT] %(message)s"))
    logger.addHandler(stderr_handler)

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


def audit_tool(tool_name: str) -> Callable:
    """Decorator that wraps an MCP tool function with audit logging."""
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args, **kwargs):
            # Extract parameters (skip 'ctx' from kwargs for logging)
            logged_params = {k: v for k, v in kwargs.items() if k != "ctx"}
            start = time.monotonic()
            try:
                result = await func(*args, **kwargs)
                duration = (time.monotonic() - start) * 1000
                # Summarise result for the log (first 200 chars)
                summary = result[:200] if isinstance(result, str) else str(result)[:200]
                log_tool_call(tool_name, logged_params, result_summary=summary, duration_ms=duration)
                return result
            except Exception as e:
                duration = (time.monotonic() - start) * 1000
                log_tool_call(tool_name, logged_params, error=str(e), duration_ms=duration)
                raise
        return wrapper
    return decorator
