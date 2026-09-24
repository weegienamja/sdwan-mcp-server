"""Cross-cutting contracts shared by every MCP tool."""

from __future__ import annotations

import inspect

import pytest

from cisco_vmanage_mcp import __version__
from cisco_vmanage_mcp.server import mcp
from cisco_vmanage_mcp.tools import (
    alarm_tools,
    config_tools,
    device_tools,
    diagnostic_tools,
    health_tools,
    policy_tools,
    tunnel_tools,
    version_tools,
)

TOOL_MODULES = (
    alarm_tools,
    config_tools,
    device_tools,
    diagnostic_tools,
    health_tools,
    policy_tools,
    tunnel_tools,
    version_tools,
)


def _tool_functions():
    for module in TOOL_MODULES:
        for name, function in inspect.getmembers(module, inspect.iscoroutinefunction):
            if name.startswith("vmanage_"):
                yield name, function


@pytest.mark.asyncio
async def test_runtime_registry_exposes_21_read_only_tools() -> None:
    tools = await mcp.list_tools()

    assert len(tools) == 21
    assert len({tool.name for tool in tools}) == 21
    for tool in tools:
        assert tool.annotations is not None
        assert tool.annotations.readOnlyHint is True
        assert tool.annotations.destructiveHint is False
        assert "response_format" in tool.inputSchema["properties"]


def test_mcp_server_reports_package_version() -> None:
    assert mcp._mcp_server.version == __version__


def test_every_tool_has_audit_wrapper() -> None:
    tools = list(_tool_functions())

    assert len(tools) == 21
    missing = [
        name
        for name, function in tools
        if getattr(function, "__audit_tool_name__", None) != name
    ]
    assert missing == []
