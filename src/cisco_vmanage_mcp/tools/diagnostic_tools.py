"""Advanced diagnostic and correlation MCP tools.

These tools compute health signals in Python and provide the LLM with
structured, grounded results. The LLM explains the computed signals;
it does not improvise conclusions from raw API data.

Each response includes:
- Computed health signals with severity
- Root-cause hypotheses with confidence levels
- Impact/blast radius assessment
- Data source citations for every conclusion
- Explicit uncertainty when data is incomplete
"""

import json

from mcp.server.fastmcp import Context

from cisco_vmanage_mcp.server import mcp
from cisco_vmanage_mcp.services.audit import audit_tool
from cisco_vmanage_mcp.services.correlation import correlate_fabric_state, diagnose_device
from cisco_vmanage_mcp.services.health_check import (
    assess_fabric_health,
)
from cisco_vmanage_mcp.tools import read_only_annotations
from cisco_vmanage_mcp.utils.errors import handle_api_error


@mcp.tool(
    name="vmanage_assess_fabric_health",
    annotations=read_only_annotations("Assess Fabric Health (Correlated)"),
)
@audit_tool("vmanage_assess_fabric_health")
async def vmanage_assess_fabric_health(
    ctx: Context,
    response_format: str = "markdown",
) -> str:
    """Perform a correlated fabric health assessment with root-cause analysis.

    This is the most comprehensive health check. Unlike vmanage_get_fabric_summary
    (which retrieves and formats data), this tool:
    1. Collects data from multiple endpoints concurrently
    2. Computes health signals for every device (not just counts)
    3. Correlates failures across devices and sites
    4. Estimates blast radius (isolated device vs site vs fabric-wide)
    5. Generates ranked root-cause hypotheses
    6. Reports data completeness (which sources succeeded/failed)

    Every conclusion is grounded in a specific API source. If any data source
    fails, the response explicitly states which conclusions may be incomplete.

    Use this as the primary fabric health check. Use vmanage_get_fabric_summary
    only when you need a quick count-based overview.
    """
    try:
        vmanage = ctx.request_context.lifespan_context["vmanage"]
        report = await correlate_fabric_state(vmanage)

        if response_format == "json":
            return json.dumps(
                {
                    "overall_health": report.fabric_report.overall_health.value,
                    "partial": report.fabric_report.partial,
                    "incomplete_sources": report.fabric_report.incomplete_sources,
                    "devices": [
                        {
                            "hostname": d.hostname,
                            "system_ip": d.system_ip,
                            "site_id": d.site_id,
                            "device_type": d.device_type,
                            "reachable": d.reachable,
                            "health": d.overall_health.value,
                            "bfd_sessions": d.bfd_sessions,
                            "control_connections": d.control_connections,
                            "signals": [
                                {
                                    "level": s.level.value,
                                    "summary": s.summary,
                                    "source": s.source.value,
                                }
                                for s in d.signals
                            ],
                        }
                        for d in report.fabric_report.devices
                    ],
                    "alarm_counts": report.fabric_report.alarm_counts,
                    "impact": {
                        "scope": report.impact.scope,
                        "affected_sites": report.impact.affected_sites,
                        "affected_devices": report.impact.affected_devices,
                        "user_impact": report.impact.estimated_user_impact,
                    } if report.impact else None,
                    "root_causes": [
                        {
                            "rank": rc.rank,
                            "hypothesis": rc.hypothesis,
                            "confidence": rc.confidence,
                            "evidence": rc.supporting_evidence,
                            "suggested_checks": rc.suggested_checks,
                        }
                        for rc in report.root_causes
                    ],
                    "data_sources": [
                        {
                            "source": fr.source.value,
                            "success": fr.success,
                            "duration_ms": fr.duration_ms,
                            "error": fr.error,
                        }
                        for fr in report.fabric_report.fetch_results
                    ],
                },
                indent=2,
            )
        else:
            return report.narrative

    except Exception as e:
        return handle_api_error(e)


@mcp.tool(
    name="vmanage_diagnose_device",
    annotations=read_only_annotations("Diagnose Device (Deep Analysis)"),
)
@audit_tool("vmanage_diagnose_device")
async def vmanage_diagnose_device(
    system_ip: str,
    ctx: Context,
    response_format: str = "markdown",
) -> str:
    """Deep diagnosis for a specific device with fabric-wide context.

    Fetches device-specific data (BFD sessions, control connections,
    system resources) AND fabric-wide data concurrently, then correlates
    to determine:
    - Is this device's issue isolated or part of a wider failure?
    - What is the blast radius?
    - What are the most likely root causes?
    - What should the operator check first?

    Unlike vmanage_get_device_status (which retrieves raw fields), this tool
    computes health signals and provides actionable diagnosis.

    Requires a valid system IP. Use vmanage_list_devices to find them.
    """
    try:
        vmanage = ctx.request_context.lifespan_context["vmanage"]
        correlation, device_health = await diagnose_device(vmanage, system_ip)

        if device_health is None:
            return (
                f"Error: No device found with system IP '{system_ip}'. "
                f"Use vmanage_list_devices to find valid system IPs."
            )

        if response_format == "json":
            return json.dumps(
                {
                    "device": {
                        "hostname": device_health.hostname,
                        "system_ip": device_health.system_ip,
                        "site_id": device_health.site_id,
                        "device_type": device_health.device_type,
                        "device_model": device_health.device_model,
                        "reachable": device_health.reachable,
                        "health": device_health.overall_health.value,
                        "bfd_sessions": device_health.bfd_sessions,
                        "control_connections": device_health.control_connections,
                        "signals": [
                            {
                                "level": s.level.value,
                                "component": s.component,
                                "summary": s.summary,
                                "detail": s.detail,
                                "source": s.source.value,
                            }
                            for s in device_health.signals
                        ],
                    },
                    "fabric_context": {
                        "overall_health": correlation.fabric_report.overall_health.value,
                        "total_devices": len(correlation.fabric_report.devices),
                        "unreachable_count": sum(
                            1 for d in correlation.fabric_report.devices if not d.reachable
                        ),
                    },
                    "impact": {
                        "scope": correlation.impact.scope,
                        "user_impact": correlation.impact.estimated_user_impact,
                    } if correlation.impact else None,
                    "root_causes": [
                        {
                            "rank": rc.rank,
                            "hypothesis": rc.hypothesis,
                            "confidence": rc.confidence,
                            "suggested_checks": rc.suggested_checks,
                        }
                        for rc in correlation.root_causes
                    ],
                    "data_sources": correlation.data_sources_used,
                },
                indent=2,
            )
        else:
            return correlation.narrative

    except Exception as e:
        return handle_api_error(e)


@mcp.tool(
    name="vmanage_pre_change_validation",
    annotations=read_only_annotations("Pre-Change Validation"),
)
@audit_tool("vmanage_pre_change_validation")
async def vmanage_pre_change_validation(
    ctx: Context,
    response_format: str = "markdown",
) -> str:
    """Validate fabric health before making configuration changes.

    Checks whether the fabric is healthy enough to safely push changes.
    Returns a clear go/no-go recommendation based on:
    - Are all controllers reachable?
    - Are there any critical alarms?
    - Are any WAN edges unreachable?
    - Are BFD/control sessions healthy?

    Run this BEFORE pushing policy changes, template updates, or
    configuration modifications.
    """
    try:
        vmanage = ctx.request_context.lifespan_context["vmanage"]
        report = await assess_fabric_health(vmanage)

        blockers: list[str] = []
        warnings: list[str] = []

        if report.partial:
            blockers.append(
                "Assessment is incomplete; required data sources could not be verified"
            )
        if not report.devices:
            blockers.append("No devices were returned by vManage")

        # Check controllers
        controllers = [d for d in report.devices if d.device_type != "vedge"]
        unreachable_controllers = [d for d in controllers if not d.reachable]
        if unreachable_controllers:
            blockers.append(
                f"Controller(s) unreachable: "
                f"{', '.join(d.hostname for d in unreachable_controllers)}"
            )

        # Check critical alarms
        critical = report.alarm_counts.get("Critical", 0)
        if critical > 0:
            blockers.append(f"{critical} critical alarm(s) active")

        # Check WAN edges
        wan_edges = [d for d in report.devices if d.device_type == "vedge"]
        unreachable_edges = [d for d in wan_edges if not d.reachable]
        if unreachable_edges:
            warnings.append(
                f"{len(unreachable_edges)} WAN edge(s) unreachable: "
                f"{', '.join(d.hostname for d in unreachable_edges)}"
            )

        # Check major alarms
        major = report.alarm_counts.get("Major", 0)
        if major > 0:
            warnings.append(f"{major} major alarm(s) active")

        # Determine recommendation
        if blockers:
            recommendation = "NO-GO"
            reason = "Critical issues must be resolved before making changes."
        elif warnings:
            recommendation = "PROCEED WITH CAUTION"
            reason = "Non-critical issues detected. Changes may be safe but require awareness."
        else:
            recommendation = "GO"
            reason = "Fabric is healthy. Safe to proceed with changes."

        if response_format == "json":
            return json.dumps(
                {
                    "recommendation": recommendation,
                    "reason": reason,
                    "blockers": blockers,
                    "warnings": warnings,
                    "fabric_health": report.overall_health.value,
                    "partial": report.partial,
                    "incomplete_sources": report.incomplete_sources,
                    "controllers_reachable": len(controllers) - len(unreachable_controllers),
                    "controllers_total": len(controllers),
                    "wan_edges_reachable": len(wan_edges) - len(unreachable_edges),
                    "wan_edges_total": len(wan_edges),
                    "alarm_counts": report.alarm_counts,
                    "data_sources": [
                        {"source": fr.source.value, "success": fr.success}
                        for fr in report.fetch_results
                    ],
                },
                indent=2,
            )

        # Markdown
        lines = [f"## Pre-Change Validation: **{recommendation}**", ""]

        if report.partial:
            lines.append(
                f"**Warning:** Assessment is partial. Data from "
                f"{', '.join(report.incomplete_sources)} could not be retrieved."
            )
            lines.append("")

        lines.append(f"**Recommendation:** {reason}")
        lines.append("")

        if blockers:
            lines.append("### Blockers (must resolve)")
            for b in blockers:
                lines.append(f"- [!] {b}")
            lines.append("")

        if warnings:
            lines.append("### Warnings (proceed with awareness)")
            for w in warnings:
                lines.append(f"- [~] {w}")
            lines.append("")

        lines.append("### Fabric Status")
        lines.append(
            f"- Controllers: {len(controllers) - len(unreachable_controllers)}/{len(controllers)} reachable"
        )
        lines.append(
            f"- WAN Edges: {len(wan_edges) - len(unreachable_edges)}/{len(wan_edges)} reachable"
        )
        for sev in ["Critical", "Major", "Medium", "Minor"]:
            count = report.alarm_counts.get(sev, 0)
            if count > 0:
                lines.append(f"- {sev} Alarms: {count}")

        lines.append("")
        lines.append("### Data Sources")
        for fr in report.fetch_results:
            status = "OK" if fr.success else f"FAILED ({fr.error})"
            lines.append(f"- {fr.source.value}: {status}")

        return "\n".join(lines)

    except Exception as e:
        return handle_api_error(e)


@mcp.tool(
    name="vmanage_incident_summary",
    annotations=read_only_annotations("Incident Summary"),
)
@audit_tool("vmanage_incident_summary")
async def vmanage_incident_summary(
    ctx: Context,
    audience: str = "engineer",
    response_format: str = "markdown",
) -> str:
    """Generate an incident summary for a specific audience.

    Provides different levels of detail based on the target audience:
    - 'executive': 3-5 line high-level summary for leadership
    - 'engineer': Detailed technical summary with device names,
      sessions, transports, and alarms

    Always includes the fabric health status, impact scope, and
    key findings. Engineer summaries include specific device details
    and suggested remediation steps.
    """
    try:
        vmanage = ctx.request_context.lifespan_context["vmanage"]
        report = await correlate_fabric_state(vmanage)

        fab = report.fabric_report
        total_devices = len(fab.devices)
        unreachable = [d for d in fab.devices if not d.reachable]
        controllers = [d for d in fab.devices if d.device_type != "vedge"]
        wan_edges = [d for d in fab.devices if d.device_type == "vedge"]

        if audience == "executive":
            # Concise executive summary
            lines = ["## SD-WAN Fabric Status Summary", ""]

            if fab.overall_health.value == "healthy":
                lines.append(
                    f"The SD-WAN fabric is **healthy**. All {len(controllers)} controllers "
                    f"and {sum(1 for w in wan_edges if w.reachable)}/{len(wan_edges)} WAN edges "
                    f"are operational."
                )
            else:
                lines.append(
                    f"The SD-WAN fabric has **{fab.overall_health.value}** status. "
                    f"{len(unreachable)} of {total_devices} devices are affected."
                )

            if report.impact and report.impact.scope != "none":
                lines.append(f"Impact is **{report.impact.scope}**-level. {report.impact.estimated_user_impact}")

            crit = fab.alarm_counts.get("Critical", 0)
            major = fab.alarm_counts.get("Major", 0)
            if crit > 0 or major > 0:
                lines.append(f"Active alarms: {crit} critical, {major} major.")

            if report.root_causes:
                top = report.root_causes[0]
                lines.append(f"Most likely cause: {top.hypothesis}.")

            if fab.partial:
                lines.append(
                    "Note: This assessment is partial due to data retrieval failures."
                )

        else:
            # Detailed engineer summary
            lines = ["## SD-WAN Incident Report (Engineer)", ""]

            lines.append(f"**Fabric Health:** {fab.overall_health.value.upper()}")
            lines.append(f"**Data Completeness:** {'Partial' if fab.partial else 'Full'}")
            lines.append("")

            lines.append("### Device Inventory")
            lines.append(f"- Controllers: {len(controllers)} ({sum(1 for c in controllers if c.reachable)} reachable)")
            lines.append(f"- WAN Edges: {len(wan_edges)} ({sum(1 for w in wan_edges if w.reachable)} reachable)")
            lines.append(f"- Total BFD: {sum(d.bfd_sessions for d in wan_edges)}")
            lines.append("")

            if unreachable:
                lines.append("### Unreachable Devices")
                for d in unreachable:
                    lines.append(
                        f"- **{d.hostname}** ({d.system_ip}) | site {d.site_id} | "
                        f"model {d.device_model} | BFD: {d.bfd_sessions} | Control: {d.control_connections}"
                    )
                lines.append("")

            if fab.alarm_counts:
                lines.append("### Alarms")
                for sev in ["Critical", "Major", "Medium", "Minor"]:
                    count = fab.alarm_counts.get(sev, 0)
                    if count > 0:
                        lines.append(f"- {sev}: {count}")
                lines.append("")

            if report.impact and report.impact.scope != "none":
                lines.append("### Impact Assessment")
                lines.append(f"- Scope: **{report.impact.scope}**")
                lines.append(f"- {report.impact.estimated_user_impact}")
                lines.append("")

            if report.root_causes:
                lines.append("### Root-Cause Analysis")
                for rc in report.root_causes:
                    lines.append(f"**{rc.rank}. {rc.hypothesis}** (confidence: {rc.confidence})")
                    for ev in rc.supporting_evidence:
                        if ev:
                            lines.append(f"   - {ev}")
                    if rc.suggested_checks:
                        lines.append(f"   - Next steps: {'; '.join(rc.suggested_checks)}")
                lines.append("")

            # Device signals
            devices_with_signals = [d for d in fab.devices if d.signals]
            if devices_with_signals:
                lines.append("### Device-Level Signals")
                for d in devices_with_signals:
                    lines.append(f"**{d.hostname}** ({d.system_ip})")
                    for s in d.signals:
                        lines.append(f"  - [{s.level.value.upper()}] {s.summary} (source: {s.source.value})")
                lines.append("")

            lines.append("### Data Sources")
            for fr in fab.fetch_results:
                status = "OK" if fr.success else f"FAILED ({fr.error})"
                lines.append(f"- {fr.source.value}: {status} ({fr.duration_ms:.0f}ms)")

        if response_format == "json":
            return json.dumps(
                {
                    "audience": audience,
                    "summary": "\n".join(lines),
                    "overall_health": fab.overall_health.value,
                    "total_devices": total_devices,
                    "unreachable_count": len(unreachable),
                    "partial": fab.partial,
                },
                indent=2,
            )

        return "\n".join(lines)

    except Exception as e:
        return handle_api_error(e)
