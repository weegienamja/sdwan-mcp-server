"""Cisco vManage MCP tools."""

from mcp.types import ToolAnnotations


def read_only_annotations(title: str) -> ToolAnnotations:
	"""Return the standard annotations shared by every vManage tool."""
	return ToolAnnotations(
		title=title,
		readOnlyHint=True,
		destructiveHint=False,
		idempotentHint=True,
		openWorldHint=True,
	)
