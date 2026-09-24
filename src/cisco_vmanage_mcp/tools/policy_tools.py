"""Policy and template MCP tools."""

import json

from mcp.server.fastmcp import Context

from cisco_vmanage_mcp.server import mcp
from cisco_vmanage_mcp.services.audit import audit_tool
from cisco_vmanage_mcp.tools import read_only_annotations
from cisco_vmanage_mcp.utils.errors import handle_api_error
from cisco_vmanage_mcp.utils.formatters import (
    _safe_str,
    format_policies_markdown,
    format_templates_markdown,
)


@mcp.tool(
    name="vmanage_list_policies",
    annotations=read_only_annotations("List vSmart Policies"),
)
@audit_tool("vmanage_list_policies")
async def vmanage_list_policies(
    ctx: Context,
    limit: int = 25,
    offset: int = 0,
    response_format: str = "markdown",
) -> str:
    """List vSmart policies with name, description, type, and activation status.

    vSmart policies control traffic routing, security, and access across the
    SD-WAN fabric. Use this to see what policies are defined and which are active.
    """
    try:
        vmanage = ctx.request_context.lifespan_context["vmanage"]
        data = await vmanage.get("/dataservice/template/policy/vsmart")

        policies = data.get("data", [])
        total = len(policies)
        policies = policies[offset : offset + limit]

        if response_format == "json":
            return json.dumps(
                {
                    "total": total,
                    "count": len(policies),
                    "offset": offset,
                    "has_more": total > offset + len(policies),
                    "policies": [
                        {
                            "name": _safe_str(p.get("policyName")),
                            "description": _safe_str(p.get("policyDescription")),
                            "type": _safe_str(p.get("policyType")),
                            "active": p.get("isPolicyActivated", False),
                            "policy_id": _safe_str(p.get("policyId")),
                        }
                        for p in policies
                    ],
                },
                indent=2,
            )
        else:
            return format_policies_markdown(policies, total, offset)

    except Exception as e:
        return handle_api_error(e)


@mcp.tool(
    name="vmanage_list_templates",
    annotations=read_only_annotations("List Device Templates"),
)
@audit_tool("vmanage_list_templates")
async def vmanage_list_templates(
    ctx: Context,
    device_type: str | None = None,
    limit: int = 25,
    offset: int = 0,
    response_format: str = "markdown",
) -> str:
    """List device templates with name, description, device type, and attached device count.

    Device templates define the configuration applied to SD-WAN devices.
    """
    try:
        vmanage = ctx.request_context.lifespan_context["vmanage"]
        data = await vmanage.get("/dataservice/template/device")

        templates = data.get("data", [])

        if device_type:
            templates = [
                t for t in templates
                if t.get("deviceType", "").lower() == device_type.lower()
            ]

        total = len(templates)
        templates = templates[offset : offset + limit]

        if response_format == "json":
            return json.dumps(
                {
                    "total": total,
                    "count": len(templates),
                    "offset": offset,
                    "has_more": total > offset + len(templates),
                    "templates": [
                        {
                            "name": _safe_str(t.get("templateName")),
                            "description": _safe_str(t.get("templateDescription")),
                            "device_type": _safe_str(t.get("deviceType")),
                            "devices_attached": _safe_str(t.get("devicesAttached")),
                            "template_id": _safe_str(t.get("templateId")),
                        }
                        for t in templates
                    ],
                },
                indent=2,
            )
        else:
            return format_templates_markdown(templates, total, offset)

    except Exception as e:
        return handle_api_error(e)
