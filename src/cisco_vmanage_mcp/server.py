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

# Import and register all tools
# These imports must come AFTER mcp is defined since the tool modules
# import mcp and use the @mcp.tool() decorator
from cisco_vmanage_mcp.tools import (
    alarm_tools,  # noqa: F401, E402
    config_tools,  # noqa: F401, E402
    device_tools,  # noqa: F401, E402
    diagnostic_tools,  # noqa: F401, E402
    health_tools,  # noqa: F401, E402
    policy_tools,  # noqa: F401, E402
    tunnel_tools,  # noqa: F401, E402
    version_tools,  # noqa: F401, E402
)


def main():
    """Entry point for the MCP server."""
    mcp.run()


if __name__ == "__main__":
    main()
