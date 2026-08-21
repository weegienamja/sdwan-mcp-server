"""Alarm and event MCP tools."""

import json
from typing import Optional
from mcp.server.fastmcp import Context

from cisco_vmanage_mcp.server import mcp
from cisco_vmanage_mcp.models.common import ResponseFormat
from cisco_vmanage_mcp.utils.formatters import (
    format_alarm_markdown,
    format_alarm_count_markdown,
    format_events_markdown,
    _safe_str,
)
from cisco_vmanage_mcp.utils.errors import handle_api_error


@mcp.tool(
    name="vmanage_list_alarms",
    annotations={
        "title": "List Active Alarms",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def vmanage_list_alarms(
    ctx: Context,
    severity: Optional[str] = None,
    hours_back: int = 24,
    limit: int = 25,
    offset: int = 0,
    response_format: str = "markdown",
) -> str:
    """List active alarms across the SD-WAN fabric.

    Returns alarms with severity, type, affected device, and timestamp.
    Sorted by severity (Critical first) then by time.

    Severity levels: Critical, Major, Medium, Minor.
    """
    try:
        vmanage = ctx.request_context.lifespan_context["vmanage"]

        # Use GET for alarms; apply time filter client-side
        data = await vmanage.get("/dataservice/alarms")

        alarms = data.get("data", [])

        # Filter by time if hours_back specified
        if hours_back:
            import time
            cutoff = int(time.time() * 1000) - (hours_back * 3600 * 1000)
            alarms = [
                a for a in alarms
                if int(a.get("entry_time", 0) or 0) >= cutoff
            ]

        if severity:
            alarms = [a for a in alarms if a.get("severity") == severity]

        severity_order = {"Critical": 0, "Major": 1, "Medium": 2, "Minor": 3}
        alarms.sort(key=lambda a: (severity_order.get(a.get("severity", ""), 4), a.get("entry_time", "")))

        total = len(alarms)
        alarms = alarms[offset : offset + limit]

        if response_format == "json":
            return json.dumps(
                {
                    "total": total,
                    "count": len(alarms),
                    "offset": offset,
                    "has_more": total > offset + len(alarms),
                    "alarms": [
                        {
                            "severity": a.get("severity", "N/A"),
                            "type": a.get("type", "N/A"),
                            "hostname": a.get("host-name", "N/A"),
                            "system_ip": a.get("system-ip", "N/A"),
                            "active_time": a.get("active-time", "N/A"),
                            "message": a.get("message", "N/A"),
                        }
                        for a in alarms
                    ],
                },
                indent=2,
            )
        else:
            return format_alarm_markdown(alarms, total)

    except Exception as e:
        return handle_api_error(e)


@mcp.tool(
    name="vmanage_get_alarm_count",
    annotations={
        "title": "Get Alarm Counts",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def vmanage_get_alarm_count(
    ctx: Context,
    response_format: str = "markdown",
) -> str:
    """Get alarm count by severity level. Useful for quick health checks.

    Returns counts for Critical, Major, Medium, and Minor alarms.
    """
    try:
        vmanage = ctx.request_context.lifespan_context["vmanage"]
        data = await vmanage.get("/dataservice/alarms/count")

        raw_counts = data.get("data", [])
        counts = {}
        if raw_counts:
            if "severity" in raw_counts[0]:
                for item in raw_counts:
                    sev = item.get("severity", "Unknown")
                    count = item.get("count", 0)
                    counts[sev] = count
            else:
                counts["Total Active"] = raw_counts[0].get("count", 0)
                counts["Total Cleared"] = raw_counts[0].get("cleared_count", 0)

        if response_format == "json":
            return json.dumps({"alarm_counts": counts}, indent=2)
        else:
            return format_alarm_count_markdown(counts)

    except Exception as e:
        return handle_api_error(e)


@mcp.tool(
    name="vmanage_list_events",
    annotations={
        "title": "List System Events",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def vmanage_list_events(
    ctx: Context,
    hours_back: int = 24,
    system_ip: Optional[str] = None,
    limit: int = 25,
    offset: int = 0,
    response_format: str = "markdown",
) -> str:
    """List recent system events (config changes, reboots, tunnel events).

    Can filter by time range and optionally by a specific device system IP.
    """
    try:
        vmanage = ctx.request_context.lifespan_context["vmanage"]

        query_params: dict = {}
        if system_ip:
            query_params["deviceId"] = system_ip

        data = await vmanage.get("/dataservice/event", params=query_params if query_params else None)

        events = data.get("data", [])

        if hours_back:
            import time
            cutoff = int(time.time() * 1000) - (hours_back * 3600 * 1000)
            events = [
                e for e in events
                if int(e.get("entry_time", e.get("receive_time", 0)) or 0) >= cutoff
            ]

        total = len(events)
        events = events[offset : offset + limit]

        if response_format == "json":
            return json.dumps(
                {
                    "total": total,
                    "count": len(events),
                    "offset": offset,
                    "has_more": total > offset + len(events),
                    "events": [
                        {
                            "event_name": _safe_str(e.get("eventname", e.get("type"))),
                            "severity": _safe_str(e.get("severity")),
                            "hostname": _safe_str(e.get("host-name")),
                            "system_ip": _safe_str(e.get("system-ip")),
                            "entry_time": _safe_str(e.get("entry_time", e.get("receive_time"))),
                        }
                        for e in events
                    ],
                },
                indent=2,
            )
        else:
            return format_events_markdown(events, total)

    except Exception as e:
        return handle_api_error(e)
