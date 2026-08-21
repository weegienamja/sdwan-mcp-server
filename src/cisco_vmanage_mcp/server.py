"""FastMCP server definition and tool registration."""

import logging
from contextlib import asynccontextmanager
from mcp.server.fastmcp import FastMCP
from cisco_vmanage_mcp.client import VManageClient

# Suppress noisy HTTP request logs that pollute MCP stdio transport
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


@asynccontextmanager
async def app_lifespan(server: FastMCP):
    """Initialise and tear down the vManage client."""
    client = VManageClient()
    # Don't authenticate eagerly — the client.get() method
    # handles lazy auth and re-auth on session expiry.
    try:
        yield {"vmanage": client}
    finally:
        await client.close()


mcp = FastMCP(
    "cisco_vmanage_mcp",
    lifespan=app_lifespan,
)

# Import and register all tools
# These imports must come AFTER mcp is defined since the tool modules
# import mcp and use the @mcp.tool() decorator
from cisco_vmanage_mcp.tools import device_tools       # noqa: F401, E402
from cisco_vmanage_mcp.tools import tunnel_tools        # noqa: F401, E402
from cisco_vmanage_mcp.tools import alarm_tools         # noqa: F401, E402
from cisco_vmanage_mcp.tools import policy_tools        # noqa: F401, E402
from cisco_vmanage_mcp.tools import config_tools        # noqa: F401, E402
from cisco_vmanage_mcp.tools import health_tools        # noqa: F401, E402
from cisco_vmanage_mcp.tools import diagnostic_tools    # noqa: F401, E402
from cisco_vmanage_mcp.tools import version_tools       # noqa: F401, E402


def main():
    """Entry point for the MCP server."""
    mcp.run()


if __name__ == "__main__":
    main()
