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
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from typing import Any

from cisco_vmanage_mcp import __version__
from cisco_vmanage_mcp.utils.private_files import write_private_text

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
        if INSTALLATION_ID_FILE.exists():
            existing = INSTALLATION_ID_FILE.read_text(encoding="ascii").strip()
            if len(existing) == 64 and all(char in "0123456789abcdef" for char in existing):
                if os.name != "nt":
                    INSTALLATION_ID_FILE.parent.chmod(0o700)
                    INSTALLATION_ID_FILE.chmod(0o600)
                return existing

        installation_id = secrets.token_hex(32)
        write_private_text(INSTALLATION_ID_FILE, installation_id + "\n", restrict_parent=True)
        return installation_id
    except OSError:
        return _EPHEMERAL_INSTALLATION_ID


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
        "server_version": __version__,
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
            version=__version__,
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
