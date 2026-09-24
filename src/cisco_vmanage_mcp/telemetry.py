"""Anonymous telemetry for cisco-vmanage-mcp tool usage.

Disabled by default. Enable by setting VMANAGE_MCP_TELEMETRY=true.

Collects only:
- Tool name
- Random installation identifier (not derived from user data)
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
import logging
import os
import secrets
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from functools import wraps
from importlib import import_module
from pathlib import Path
from typing import Any

logger = logging.getLogger("cisco_vmanage_mcp.telemetry")

INSTALLATION_ID_FILE = Path.home() / ".vmanage-mcp" / "installation-id"
_EPHEMERAL_INSTALLATION_ID = secrets.token_hex(32)
_ide_client: Any = None


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


def _installation_id() -> str:
    """Return a random installation ID without deriving it from user data."""
    try:
        INSTALLATION_ID_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        INSTALLATION_ID_FILE.parent.chmod(0o700)
        if INSTALLATION_ID_FILE.exists():
            existing = INSTALLATION_ID_FILE.read_text(encoding="ascii").strip()
            if len(existing) == 64 and all(char in "0123456789abcdef" for char in existing):
                INSTALLATION_ID_FILE.chmod(0o600)
                return existing

        installation_id = secrets.token_hex(32)
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(INSTALLATION_ID_FILE, flags, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            os.write(descriptor, (installation_id + "\n").encode("ascii"))
        finally:
            os.close(descriptor)
        return installation_id
    except OSError:
        return _EPHEMERAL_INSTALLATION_ID


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
        "installation_id": _installation_id(),
        "duration_ms": round(duration_ms, 2),
        "success": success,
        "server_version": _get_version(),
        "timestamp": datetime.now(UTC).isoformat(),
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


async def initialize_ide_telemetry() -> None:
    """Initialize Cisco IDE telemetry when explicitly enabled."""
    global _ide_client
    if os.getenv("VMANAGE_MCP_IDE_TELEMETRY", "false").lower() != "true":
        _ide_client = None
        return

    try:
        sdk = import_module("ide_telemetry_mcp")
        config = sdk.TelemetryConfig(
            mcp_server_name="cisco-vmanage-mcp",
            mcp_marketplace_id=os.getenv("IDE_MCP_MARKETPLACE_ID"),
            version=_get_version(),
            group_name=os.getenv("IDE_TELEMETRY_GROUP"),
            app_name=os.getenv("IDE_TELEMETRY_APP"),
        )
        _ide_client = await sdk.TelemetryClient.get_instance(
            telemetry_config=config,
        )
    except Exception as exc:
        _ide_client = None
        logger.warning("Cisco IDE telemetry initialization failed: %s", exc)


async def emit_ide_tool_event(
    tool_name: str,
    duration_ms: float,
    success: bool,
) -> None:
    """Emit a minimal tool event through the optional Cisco IDE client."""
    if _ide_client is None:
        return
    try:
        await asyncio.wait_for(
            _ide_client.send_event(
                tool_name,
                {
                    "durationMs": round(duration_ms, 2),
                    "success": success,
                },
                user_id=None,
            ),
            timeout=float(os.getenv("IDE_TELEMETRY_TIMEOUT", "2.0")),
        )
    except Exception as exc:
        logger.debug("Cisco IDE telemetry event failed: %s", exc)


async def shutdown_ide_telemetry() -> None:
    """Close the optional Cisco IDE telemetry client."""
    global _ide_client
    client = _ide_client
    _ide_client = None
    if client is None:
        return
    try:
        await client.shutdown()
    except Exception as exc:
        logger.debug("Cisco IDE telemetry shutdown failed: %s", exc)
