"""Device inventory and status MCP tools."""

import json
from typing import Optional
from mcp.server.fastmcp import Context

from cisco_vmanage_mcp.server import mcp
from cisco_vmanage_mcp.models.common import ResponseFormat
from cisco_vmanage_mcp.utils.formatters import (
    format_device_table_markdown,
    format_device_status_markdown,
    format_interfaces_markdown,
    format_counters_markdown,
    _ms_to_readable,
    _safe_str,
)
from cisco_vmanage_mcp.utils.errors import handle_api_error


@mcp.tool(
    name="vmanage_list_devices",
    annotations={
        "title": "List SD-WAN Devices",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def vmanage_list_devices(
    ctx: Context,
    device_type: Optional[str] = None,
    device_model: Optional[str] = None,
    reachability: Optional[str] = None,
    limit: int = 25,
    offset: int = 0,
    response_format: str = "markdown",
) -> str:
    """List all devices in the Cisco SD-WAN fabric with their status.

    Returns device inventory including hostname, system IP, device model,
    reachability status, software version, site ID, BFD sessions, and
    control connections. Use this tool first to discover devices before
    querying specific device details.

    NOTE: Both cEdge and vEdge routers show device_type='vedge'. To tell
    them apart, check device_model: 'vedge-C8000V' = cEdge, 'vedge-cloud' = vEdge.
    """
    try:
        vmanage = ctx.request_context.lifespan_context["vmanage"]
        data = await vmanage.get("/dataservice/device")

        devices = data.get("data", [])

        if device_type:
            devices = [
                d for d in devices
                if d.get("device-type", "").lower() == device_type.lower()
            ]

        if device_model:
            devices = [
                d for d in devices
                if d.get("device-model", "").lower() == device_model.lower()
            ]

        if reachability:
            devices = [
                d for d in devices
                if d.get("reachability", "").lower() == reachability.lower()
            ]

        total = len(devices)
        devices = devices[offset : offset + limit]

        if response_format == "json":
            return json.dumps(
                {
                    "total": total,
                    "count": len(devices),
                    "offset": offset,
                    "has_more": total > offset + len(devices),
                    "devices": [
                        {
                            "hostname": d.get("host-name", "N/A"),
                            "system_ip": d.get("system-ip", "N/A"),
                            "device_type": d.get("device-type", "N/A"),
                            "device_model": d.get("device-model", "N/A"),
                            "reachability": d.get("reachability", "N/A"),
                            "version": d.get("version", "N/A"),
                            "site_id": d.get("site-id", "N/A"),
                            "uuid": d.get("uuid", "N/A"),
                            "state": d.get("state", "N/A"),
                            "state_description": d.get("state_description", "N/A"),
                            "bfd_sessions": d.get("bfdSessions", "--"),
                            "control_connections": d.get("controlConnections", "--"),
                        }
                        for d in devices
                    ],
                },
                indent=2,
            )
        else:
            return format_device_table_markdown(devices, total, offset)

    except Exception as e:
        return handle_api_error(e)


@mcp.tool(
    name="vmanage_get_device_status",
    annotations={
        "title": "Get Device Status",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def vmanage_get_device_status(
    system_ip: str,
    ctx: Context,
    response_format: str = "markdown",
) -> str:
    """Get detailed status for a specific SD-WAN device by system IP.

    Returns hostname, system IP, device model, version, uptime, board serial,
    certificate validity, state, BFD sessions, control connections, connected
    vManages, site ID, and location.

    Use vmanage_list_devices to find valid system IPs.
    """
    try:
        vmanage = ctx.request_context.lifespan_context["vmanage"]
        data = await vmanage.get("/dataservice/device")

        devices = data.get("data", [])
        device = next(
            (d for d in devices if d.get("deviceId") == system_ip or d.get("system-ip") == system_ip),
            None,
        )

        if not device:
            return f"Error: No device found with system IP '{system_ip}'. Use vmanage_list_devices to find valid system IPs."

        if response_format == "json":
            return json.dumps(
                {
                    "hostname": device.get("host-name", "N/A"),
                    "system_ip": device.get("system-ip", "N/A"),
                    "device_type": device.get("device-type", "N/A"),
                    "device_model": device.get("device-model", "N/A"),
                    "version": device.get("version", "N/A"),
                    "reachability": device.get("reachability", "N/A"),
                    "state": device.get("state", "N/A"),
                    "state_description": device.get("state_description", "N/A"),
                    "site_id": device.get("site-id", "N/A"),
                    "uuid": device.get("uuid", "N/A"),
                    "board_serial": device.get("board-serial", "N/A"),
                    "certificate_validity": device.get("certificate-validity", "N/A"),
                    "uptime_since": _ms_to_readable(device.get("uptime-date")),
                    "cpu_count": _safe_str(device.get("total_cpu_count")),
                    "bfd_sessions": _safe_str(device.get("bfdSessions")),
                    "control_connections": _safe_str(device.get("controlConnections")),
                    "connected_vmanages": device.get("connectedVManages", []),
                    "latitude": device.get("latitude"),
                    "longitude": device.get("longitude"),
                },
                indent=2,
            )
        else:
            return format_device_status_markdown(device)

    except Exception as e:
        return handle_api_error(e)


@mcp.tool(
    name="vmanage_get_device_counters",
    annotations={
        "title": "Get Device Counters",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def vmanage_get_device_counters(
    system_ip: str,
    ctx: Context,
    response_format: str = "markdown",
) -> str:
    """Get interface error counters, dropped packets, and reboot counts for a device.

    Requires the device system IP. Use vmanage_list_devices to find valid system IPs.
    """
    try:
        vmanage = ctx.request_context.lifespan_context["vmanage"]
        data = await vmanage.get(
            "/dataservice/device/counters",
            params={"deviceId": system_ip},
        )

        counters = data.get("data", [])

        if response_format == "json":
            return json.dumps({"system_ip": system_ip, "counters": counters}, indent=2)
        else:
            return format_counters_markdown(counters, system_ip)

    except Exception as e:
        return handle_api_error(e)


@mcp.tool(
    name="vmanage_get_device_interfaces",
    annotations={
        "title": "Get Device Interfaces",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def vmanage_get_device_interfaces(
    system_ip: str,
    ctx: Context,
    response_format: str = "markdown",
) -> str:
    """Get interface list for a device with status, IP, speed, and TX/RX octets.

    Requires the device system IP. Use vmanage_list_devices to find valid system IPs.
    """
    try:
        vmanage = ctx.request_context.lifespan_context["vmanage"]
        data = await vmanage.get(
            "/dataservice/device/interface",
            params={"deviceId": system_ip},
        )

        interfaces = data.get("data", [])

        if response_format == "json":
            return json.dumps(
                {
                    "system_ip": system_ip,
                    "count": len(interfaces),
                    "interfaces": [
                        {
                            "name": i.get("ifname", "N/A"),
                            "admin_status": i.get("if-admin-status", "N/A"),
                            "oper_status": i.get("if-oper-status", "N/A"),
                            "ip_address": i.get("ip-address", "N/A"),
                            "speed": _safe_str(i.get("speed-mbps", i.get("if-speed"))),
                            "tx_octets": _safe_str(i.get("tx-octets")),
                            "rx_octets": _safe_str(i.get("rx-octets")),
                        }
                        for i in interfaces
                    ],
                },
                indent=2,
            )
        else:
            return format_interfaces_markdown(interfaces, system_ip)

    except Exception as e:
        return handle_api_error(e)
