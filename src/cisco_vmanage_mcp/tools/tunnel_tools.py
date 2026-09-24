"""Tunnel and BFD session MCP tools."""

import json

from mcp.server.fastmcp import Context

from cisco_vmanage_mcp.server import mcp
from cisco_vmanage_mcp.services.audit import audit_tool
from cisco_vmanage_mcp.tools import read_only_annotations
from cisco_vmanage_mcp.utils.errors import handle_api_error
from cisco_vmanage_mcp.utils.formatters import (
    _safe_str,
    format_bfd_table_markdown,
    format_omp_peers_markdown,
    format_tunnel_table_markdown,
)


@mcp.tool(
    name="vmanage_list_tunnels",
    annotations=read_only_annotations("List Device Tunnels"),
)
@audit_tool("vmanage_list_tunnels")
async def vmanage_list_tunnels(
    system_ip: str,
    ctx: Context,
    response_format: str = "markdown",
) -> str:
    """List all IPsec tunnels for a specific WAN edge device.

    Returns tunnel details including destination system IP, protocol, state,
    jitter, latency, and loss. Critical for SD-WAN health monitoring.

    Requires the device system IP of a WAN edge (vedge). Use vmanage_list_devices
    to find reachable WAN edges with active BFD sessions.
    """
    try:
        vmanage = ctx.request_context.lifespan_context["vmanage"]
        data = await vmanage.get(
            "/dataservice/device/tunnel/statistics",
            params={"deviceId": system_ip},
        )

        tunnels = data.get("data", [])

        if response_format == "json":
            return json.dumps(
                {
                    "system_ip": system_ip,
                    "count": len(tunnels),
                    "tunnels": [
                        {
                            "dest_ip": _safe_str(t.get("dest-ip")),
                            "system_ip": _safe_str(t.get("system-ip")),
                            "tunnel_protocol": _safe_str(t.get("tunnel-protocol")),
                            "source_ip": _safe_str(t.get("source-ip")),
                            "local_color": _safe_str(t.get("local-color")),
                            "remote_color": _safe_str(t.get("remote-color")),
                            "tx_pkts": t.get("tx_pkts"),
                            "rx_pkts": t.get("rx_pkts"),
                            "tx_octets": t.get("tx_octets"),
                            "rx_octets": t.get("rx_octets"),
                            "tunnel_mtu": t.get("tunnel-mtu"),
                        }
                        for t in tunnels
                    ],
                },
                indent=2,
            )
        else:
            return format_tunnel_table_markdown(tunnels, system_ip)

    except Exception as e:
        return handle_api_error(e)


@mcp.tool(
    name="vmanage_get_bfd_sessions",
    annotations=read_only_annotations("Get BFD Sessions"),
)
@audit_tool("vmanage_get_bfd_sessions")
async def vmanage_get_bfd_sessions(
    system_ip: str,
    ctx: Context,
    response_format: str = "markdown",
) -> str:
    """Get BFD (Bidirectional Forwarding Detection) session status for a device.

    Returns BFD peer system IP, state (up/down), source/destination TLOC colour,
    and site ID. BFD is the underlay health-check mechanism for SD-WAN tunnels.

    Requires the system IP of a WAN edge device with BFD sessions.
    """
    try:
        vmanage = ctx.request_context.lifespan_context["vmanage"]
        data = await vmanage.get(
            "/dataservice/device/bfd/sessions",
            params={"deviceId": system_ip},
        )

        sessions = data.get("data", [])

        if response_format == "json":
            return json.dumps(
                {
                    "system_ip": system_ip,
                    "count": len(sessions),
                    "sessions": [
                        {
                            "peer_system_ip": _safe_str(s.get("system-ip")),
                            "state": _safe_str(s.get("state")),
                            "src_ip": _safe_str(s.get("src-ip")),
                            "src_port": _safe_str(s.get("src-port")),
                            "dst_ip": _safe_str(s.get("dst-ip")),
                            "dst_port": _safe_str(s.get("dst-port")),
                            "site_id": _safe_str(s.get("site-id")),
                            "local_color": _safe_str(s.get("local-color")),
                            "color": _safe_str(s.get("color")),
                        }
                        for s in sessions
                    ],
                },
                indent=2,
            )
        else:
            return format_bfd_table_markdown(sessions, system_ip)

    except Exception as e:
        return handle_api_error(e)


@mcp.tool(
    name="vmanage_get_omp_peers",
    annotations=read_only_annotations("Get OMP Peers"),
)
@audit_tool("vmanage_get_omp_peers")
async def vmanage_get_omp_peers(
    system_ip: str,
    ctx: Context,
    response_format: str = "markdown",
) -> str:
    """Get OMP (Overlay Management Protocol) peer list for a device.

    Returns OMP peers with state, site ID, domain ID, and peer type.
    OMP is the control-plane protocol that distributes routes and policies
    across the SD-WAN fabric via vSmart controllers.

    Requires the system IP of a vSmart or WAN edge device.
    """
    try:
        vmanage = ctx.request_context.lifespan_context["vmanage"]
        data = await vmanage.get(
            "/dataservice/device/omp/peers",
            params={"deviceId": system_ip},
        )

        peers = data.get("data", [])

        if response_format == "json":
            return json.dumps(
                {
                    "system_ip": system_ip,
                    "count": len(peers),
                    "peers": [
                        {
                            "peer": _safe_str(p.get("peer")),
                            "state": _safe_str(p.get("state")),
                            "site_id": _safe_str(p.get("site-id")),
                            "domain_id": _safe_str(p.get("domain-id")),
                            "type": _safe_str(p.get("type")),
                        }
                        for p in peers
                    ],
                },
                indent=2,
            )
        else:
            return format_omp_peers_markdown(peers, system_ip)

    except Exception as e:
        return handle_api_error(e)
