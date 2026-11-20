"""FastMCP server definition and tool registration."""

import logging
from contextlib import asynccontextmanager

from mcp.server.fastmcp import FastMCP

from cisco_vmanage_mcp import __version__
from cisco_vmanage_mcp.client import VManageClient
from cisco_vmanage_mcp.telemetry import (
    initialize_ide_telemetry,
    shutdown_ide_telemetry,
)
from cisco_vmanage_mcp.tools import (
    alarm_tools,
    config_tools,
    device_tools,
    diagnostic_tools,
    health_tools,
    policy_tools,
    read_only_annotations,
    tunnel_tools,
    version_tools,
)

# Suppress noisy HTTP request logs that pollute MCP stdio transport
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


@asynccontextmanager
async def app_lifespan(_server: FastMCP):
    """Initialise and tear down the vManage client."""
    client = VManageClient()
    await initialize_ide_telemetry()
    # Don't authenticate eagerly — the client.get() method
    # handles lazy auth and re-auth on session expiry.
    try:
        yield {"vmanage": client}
    finally:
        await client.close()
        await shutdown_ide_telemetry()


mcp = FastMCP(
    "cisco_vmanage_mcp",
    lifespan=app_lifespan,
)
# Official FastMCP currently exposes the low-level version only after construction.
mcp._mcp_server.version = __version__

for function, title in (
    (alarm_tools.vmanage_list_alarms, "List Active Alarms"),
    (alarm_tools.vmanage_get_alarm_count, "Get Alarm Counts"),
    (alarm_tools.vmanage_list_events, "List System Events"),
    (config_tools.vmanage_get_running_config, "Get Running Config"),
    (device_tools.vmanage_list_devices, "List SD-WAN Devices"),
    (device_tools.vmanage_get_device_status, "Get Device Status"),
    (device_tools.vmanage_get_device_counters, "Get Device Counters"),
    (device_tools.vmanage_get_device_interfaces, "Get Device Interfaces"),
    (diagnostic_tools.vmanage_assess_fabric_health, "Assess Fabric Health (Correlated)"),
    (diagnostic_tools.vmanage_diagnose_device, "Diagnose Device (Deep Analysis)"),
    (diagnostic_tools.vmanage_pre_change_validation, "Pre-Change Validation"),
    (diagnostic_tools.vmanage_incident_summary, "Incident Summary"),
    (health_tools.vmanage_get_system_status, "Get System Status"),
    (health_tools.vmanage_get_control_status, "Get Control Connections"),
    (health_tools.vmanage_get_fabric_summary, "Get Fabric Summary"),
    (policy_tools.vmanage_list_policies, "List vSmart Policies"),
    (policy_tools.vmanage_list_templates, "List Device Templates"),
    (tunnel_tools.vmanage_list_tunnels, "List Device Tunnels"),
    (tunnel_tools.vmanage_get_bfd_sessions, "Get BFD Sessions"),
    (tunnel_tools.vmanage_get_omp_peers, "Get OMP Peers"),
    (version_tools.vmanage_check_version, "Check Server Version"),
):
    mcp.add_tool(function, annotations=read_only_annotations(title))


def main():
    """Entry point for the MCP server."""
    mcp.run()


if __name__ == "__main__":
    main()
