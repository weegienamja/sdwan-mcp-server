"""Version check MCP tool.

Provides a tool that returns the current server version and checks the public
GitHub repository for newer tags when it is reachable.
"""

from __future__ import annotations

import json
import logging
import re

from mcp.server.fastmcp import Context

from cisco_vmanage_mcp.server import mcp
from cisco_vmanage_mcp import __version__

logger = logging.getLogger("cisco_vmanage_mcp.version_tool")

_REPO_API_URL = "https://api.github.com/repos/weegienamja/sdwan-mcp-server/tags"


async def _fetch_latest_tag() -> str | None:
    """Fetch the latest version tag from the public GitHub repository."""
    try:
        import httpx

        async with httpx.AsyncClient(timeout=10.0, verify=True) as client:
            response = await client.get(
                _REPO_API_URL,
                headers={"Accept": "application/vnd.github+json"},
            )
            if response.status_code != 200:
                return None
            tags = response.json()
            if not tags:
                return None
            # Tags are returned newest-first by the GitHub API.
            # Find the first tag that looks like a version (v1.2.3 or 1.2.3).
            for tag in tags:
                name = tag.get("name", "")
                if re.match(r"^v?\d+\.\d+\.\d+", name):
                    return name.lstrip("v")
            return None
    except Exception as e:
        logger.debug("Failed to fetch latest tag: %s", e)
        return None


def _parse_version(version_str: str) -> tuple[int, ...]:
    """Parse a semver string into a tuple for comparison."""
    match = re.match(r"(\d+)\.(\d+)\.(\d+)", version_str)
    if match:
        return tuple(int(x) for x in match.groups())
    return (0, 0, 0)


@mcp.tool(
    name="vmanage_check_version",
    annotations={
        "title": "Check Server Version",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def vmanage_check_version(
    ctx: Context,
    response_format: str = "markdown",
) -> str:
    """Check the current server version and whether a newer public tag exists.

    Returns the running version and, when the repository is publicly reachable,
    compares it with the latest release tag.
    """
    current = __version__
    latest = await _fetch_latest_tag()

    update_available = False
    if latest:
        current_tuple = _parse_version(current)
        latest_tuple = _parse_version(latest)
        update_available = latest_tuple > current_tuple

    if response_format == "json":
        return json.dumps(
            {
                "current_version": current,
                "latest_version": latest or "unknown",
                "update_available": update_available,
            },
            indent=2,
        )

    lines = [
        "## cisco-vmanage-mcp Version",
        "",
        f"**Current:** v{current}",
    ]

    if latest:
        lines.append(f"**Latest:**  v{latest}")
        if update_available:
            lines.extend(
                [
                    "",
                    "An update is available. Upgrade with:",
                    "```",
                    "cd /path/to/sdwan-mcp-server && git pull && pip install -e .",
                    "```",
                ]
            )
        else:
            lines.extend(["", "You are running the latest version."])
    else:
        lines.append("**Latest:**  Could not check public repository tags")

    return "\n".join(lines)
