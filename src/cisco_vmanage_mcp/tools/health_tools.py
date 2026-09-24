"""System health and fabric summary MCP tools."""

import json

from mcp.server.fastmcp import Context

from cisco_vmanage_mcp.server import mcp
from cisco_vmanage_mcp.services.audit import audit_tool
from cisco_vmanage_mcp.tools import read_only_annotations
from cisco_vmanage_mcp.utils.errors import handle_api_error
from cisco_vmanage_mcp.utils.formatters import (
    _safe_str,
    format_control_connections_markdown,
    format_fabric_summary_markdown,
    format_system_status_markdown,
)


@mcp.tool(
    name="vmanage_get_system_status",
    annotations=read_only_annotations("Get System Status"),
)
@audit_tool("vmanage_get_system_status")
async def vmanage_get_system_status(
    system_ip: str,
    ctx: Context,
    response_format: str = "markdown",
) -> str:
    """Get system-level stats for a device: CPU load, memory, disk usage, uptime.

    Requires the device system IP. Use vmanage_list_devices to find valid system IPs.
    """
    try:
        vmanage = ctx.request_context.lifespan_context["vmanage"]
        data = await vmanage.get(
            "/dataservice/device/system/status",
            params={"deviceId": system_ip},
        )

        statuses = data.get("data", [])

        if response_format == "json":
            return json.dumps(
                {
                    "system_ip": system_ip,
                    "status": [
                        {
                            "cpu_load_1min": _safe_str(s.get("min1_avg")),
                            "cpu_load_5min": _safe_str(s.get("min5_avg")),
                            "cpu_load_15min": _safe_str(s.get("min15_avg")),
                            "mem_used": _safe_str(s.get("mem_used")),
                            "mem_free": _safe_str(s.get("mem_free")),
                            "disk_used": _safe_str(s.get("disk_used")),
                            "disk_avail": _safe_str(s.get("disk_avail")),
                            "uptime": _safe_str(s.get("uptime")),
                        }
                        for s in statuses
                    ],
                },
                indent=2,
            )
        else:
            return format_system_status_markdown(statuses, system_ip)

    except Exception as e:
        return handle_api_error(e)


@mcp.tool(
    name="vmanage_get_control_status",
    annotations=read_only_annotations("Get Control Connections"),
)
@audit_tool("vmanage_get_control_status")
async def vmanage_get_control_status(
    system_ip: str,
    ctx: Context,
    response_format: str = "markdown",
) -> str:
    """Get control connections for a device: peer type, peer system IP, state, uptime.

    Critical for verifying vSmart/vBond connectivity. If control connections
    are down, the device cannot receive policies or route updates.

    Requires the device system IP.
    """
    try:
        vmanage = ctx.request_context.lifespan_context["vmanage"]
        data = await vmanage.get(
            "/dataservice/device/control/connections",
            params={"deviceId": system_ip},
        )

        connections = data.get("data", [])

        if response_format == "json":
            return json.dumps(
                {
                    "system_ip": system_ip,
                    "count": len(connections),
                    "connections": [
                        {
                            "peer_type": _safe_str(c.get("peer-type")),
                            "peer_system_ip": _safe_str(c.get("system-ip")),
                            "state": _safe_str(c.get("state")),
                            "uptime": _safe_str(c.get("uptime")),
                        }
                        for c in connections
                    ],
                },
                indent=2,
            )
        else:
            return format_control_connections_markdown(connections, system_ip)

    except Exception as e:
        return handle_api_error(e)


@mcp.tool(
    name="vmanage_get_fabric_summary",
    annotations=read_only_annotations("Get Fabric Summary"),
)
@audit_tool("vmanage_get_fabric_summary")
async def vmanage_get_fabric_summary(
    ctx: Context,
    response_format: str = "markdown",
) -> str:
    """Get an overall SD-WAN fabric health summary.

    This is a composite tool that calls multiple endpoints internally and
    synthesises the results into a single overview. It returns:
    - Total devices by type (controllers vs WAN edges)
    - Reachable vs unreachable device counts
    - Device state summary (green/yellow/red)
    - Alarm counts by severity
    - Total BFD sessions across all WAN edges
    - List of any unreachable devices

    This is the best tool to call first for a quick overview of fabric health.
    """
    try:
        vmanage = ctx.request_context.lifespan_context["vmanage"]

        # Fetch device list and alarm counts (the only 2 API calls needed)
        device_data = await vmanage.get("/dataservice/device")
        alarm_data = await vmanage.get("/dataservice/alarms/count")

        devices = device_data.get("data", [])

        # Count by type
        vmanage_count = sum(1 for d in devices if d.get("device-type") == "vmanage")
        vsmart_count = sum(1 for d in devices if d.get("device-type") == "vsmart")
        vbond_count = sum(1 for d in devices if d.get("device-type") == "vbond")
        wan_edges = [d for d in devices if d.get("device-type") == "vedge"]

        controllers = vmanage_count + vsmart_count + vbond_count

        # Reachability
        wan_edges_reachable = sum(1 for d in wan_edges if d.get("reachability") == "reachable")
        wan_edges_unreachable = sum(1 for d in wan_edges if d.get("reachability") == "unreachable")

        # Device states
        state_green = sum(1 for d in devices if d.get("state") == "green")
        state_yellow = sum(1 for d in devices if d.get("state") == "yellow")
        state_red = sum(1 for d in devices if d.get("state") == "red")

        # BFD sessions from device list (no extra API calls)
        total_bfd = 0
        for d in wan_edges:
            bfd = d.get("bfdSessions", "--")
            if bfd and bfd != "--":
                try:
                    total_bfd += int(bfd)
                except (ValueError, TypeError):
                    pass

        # Unreachable devices
        unreachable_devices = [
            {
                "hostname": d.get("host-name", "N/A"),
                "system_ip": d.get("system-ip", "N/A"),
                "site_id": d.get("site-id", "N/A"),
            }
            for d in devices
            if d.get("reachability") == "unreachable"
        ]

        # Parse alarm counts -- handle both severity-keyed and flat formats
        alarm_counts = {}
        alarm_items = alarm_data.get("data", [])
        if alarm_items:
            if "severity" in alarm_items[0]:
                for item in alarm_items:
                    alarm_counts[item.get("severity", "Unknown")] = item.get("count", 0)
            else:
                alarm_counts["Total Active"] = alarm_items[0].get("count", 0)

        summary = {
            "controllers": controllers,
            "vmanage_count": vmanage_count,
            "vsmart_count": vsmart_count,
            "vbond_count": vbond_count,
            "wan_edges": len(wan_edges),
            "wan_edges_reachable": wan_edges_reachable,
            "wan_edges_unreachable": wan_edges_unreachable,
            "state_green": state_green,
            "state_yellow": state_yellow,
            "state_red": state_red,
            "alarms_critical": alarm_counts.get("Critical", 0),
            "alarms_major": alarm_counts.get("Major", 0),
            "alarms_medium": alarm_counts.get("Medium", 0),
            "alarms_minor": alarm_counts.get("Minor", 0),
            "total_bfd_sessions": total_bfd,
            "unreachable_devices": unreachable_devices,
        }

        if response_format == "json":
            return json.dumps(summary, indent=2)
        else:
            return format_fabric_summary_markdown(summary)

    except Exception as e:
        return handle_api_error(e)
