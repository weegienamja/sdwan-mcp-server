"""Anonymous telemetry for cisco-vmanage-mcp tool usage.

Disabled by default. Enable by setting VMANAGE_MCP_TELEMETRY=true.

Collects only:
- Tool name
- Anonymous user hash (SHA-256 of username, irreversible)
- Duration in milliseconds
- Success or failure boolean
- Server version
- Timestamp (ISO 8601)

Never collects: credentials, device IPs, alarm content, API response
bodies, or any PII.

Events are sent to a Splunk HEC endpoint (configurable via SPLUNK_HEC_URL
and SPLUNK_HEC_TOKEN env vars). Telemetry failures never affect tool
execution -- all sends are fire-and-forget in a background thread.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
import threading
from datetime import datetime, timezone
from functools import wraps
from typing import Any, Callable

logger = logging.getLogger("cisco_vmanage_mcp.telemetry")


def _is_enabled() -> bool:
    """Check if telemetry is enabled via environment variable."""
    return os.getenv("VMANAGE_MCP_TELEMETRY", "false").lower() == "true"


def _get_splunk_config() -> tuple[str, str] | None:
    """Return (url, token) for Splunk HEC, or None if not configured."""
    url = os.getenv("SPLUNK_HEC_URL", "").strip()
    token = os.getenv("SPLUNK_HEC_TOKEN", "").strip()
    if url and token:
        return url, token
    return None


def _anonymous_user_hash() -> str:
    """Generate irreversible SHA-256 hash of the vManage username."""
    username = os.getenv("VMANAGE_USERNAME", "unknown")
    return hashlib.sha256(username.encode("utf-8")).hexdigest()


def _get_version() -> str:
    """Get the current server version."""
    try:
        from cisco_vmanage_mcp import __version__
        return __version__
    except ImportError:
        return "unknown"


def _build_event(
    tool_name: str,
    duration_ms: float,
    success: bool,
) -> dict[str, Any]:
    """Build a telemetry event payload."""
    return {
        "tool_name": tool_name,
        "user_hash": _anonymous_user_hash(),
        "duration_ms": round(duration_ms, 2),
        "success": success,
        "server_version": _get_version(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _send_event(event: dict[str, Any]) -> None:
    """Send a telemetry event to Splunk HEC. Fire-and-forget."""
    config = _get_splunk_config()
    if config is None:
        logger.debug("Splunk HEC not configured, skipping telemetry send.")
        return

    url, token = config

    try:
        import httpx

        splunk_payload = {
            "event": event,
            "sourcetype": "cisco_vmanage_mcp",
            "source": "mcp-telemetry",
        }

        # Use a sync client with a short timeout to avoid blocking
        with httpx.Client(timeout=5.0, verify=True) as client:
            client.post(
                url,
                json=splunk_payload,
                headers={
                    "Authorization": f"Splunk {token}",
                    "Content-Type": "application/json",
                },
            )
    except Exception:
        # Telemetry must never affect tool execution
        logger.debug("Telemetry send failed (silently ignored).", exc_info=True)


def _send_in_background(event: dict[str, Any]) -> None:
    """Send telemetry event in a background thread (zero latency impact)."""
    thread = threading.Thread(target=_send_event, args=(event,), daemon=True)
    thread.start()


def emit_tool_event(tool_name: str, duration_ms: float, success: bool) -> None:
    """Emit a telemetry event if telemetry is enabled.

    Safe to call unconditionally -- returns immediately if disabled.
    """
    if not _is_enabled():
        return

    try:
        event = _build_event(tool_name, duration_ms, success)
        _send_in_background(event)
    except Exception:
        # Never let telemetry errors propagate
        logger.debug("Telemetry event creation failed (silently ignored).", exc_info=True)


def track_telemetry(tool_name: str | None = None) -> Callable:
    """Decorator that emits a telemetry event for each tool call.

    Can be used alongside the existing @audit_tool decorator:

        @mcp.tool(...)
        @audit_tool("vmanage_some_tool")
        @track_telemetry("vmanage_some_tool")
        async def vmanage_some_tool(...):
            ...

    If tool_name is not provided, uses the decorated function's name.
    """
    def decorator(func: Callable) -> Callable:
        name = tool_name or func.__name__

        @wraps(func)
        async def async_wrapper(*args, **kwargs):
            start = time.monotonic()
            success = True
            try:
                result = await func(*args, **kwargs)
                return result
            except Exception:
                success = False
                raise
            finally:
                duration_ms = (time.monotonic() - start) * 1000
                emit_tool_event(name, duration_ms, success)

        @wraps(func)
        def sync_wrapper(*args, **kwargs):
            start = time.monotonic()
            success = True
            try:
                result = func(*args, **kwargs)
                return result
            except Exception:
                success = False
                raise
            finally:
                duration_ms = (time.monotonic() - start) * 1000
                emit_tool_event(name, duration_ms, success)

        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        return sync_wrapper

    return decorator
