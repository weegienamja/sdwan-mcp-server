"""Running config MCP tools."""

import json

from mcp.server.fastmcp import Context

from cisco_vmanage_mcp.server import mcp
from cisco_vmanage_mcp.services.audit import audit_tool
from cisco_vmanage_mcp.tools import read_only_annotations
from cisco_vmanage_mcp.utils.errors import handle_api_error


@mcp.tool(
    name="vmanage_get_running_config",
    annotations=read_only_annotations("Get Running Config"),
)
@audit_tool("vmanage_get_running_config")
async def vmanage_get_running_config(
    device_uuid: str,
    ctx: Context,
    response_format: str = "markdown",
) -> str:
    """Get the running configuration for a specific device.

    Requires the device UUID, not the system IP. Use vmanage_list_devices
    first to find the UUID for a device.

    Returns the full device configuration. The response may be plain text
    (Cisco IOS-XE/Viptela config format) or JSON depending on the device type.
    """
    try:
        vmanage = ctx.request_context.lifespan_context["vmanage"]

        try:
            raw = await vmanage.get_raw(
                f"/dataservice/template/config/running/{device_uuid}"
            )
        except Exception:
            data = await vmanage.get(
                f"/dataservice/template/config/running/{device_uuid}"
            )
            raw = json.dumps(data, indent=2)

        if response_format == "json":
            return json.dumps(
                {
                    "device_uuid": device_uuid,
                    "config": raw,
                },
                indent=2,
            )
        else:
            return f"**Running Config for {device_uuid}**\n\n```\n{raw}\n```"

    except Exception as e:
        return handle_api_error(e)
