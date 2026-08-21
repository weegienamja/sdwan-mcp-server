"""CLI companion tool for cisco-vmanage-mcp.

Provides terminal commands for quick checks, scripting, and CI pipelines.
Uses the same VManageClient and service modules as the MCP server.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from pathlib import Path

import click
from dotenv import load_dotenv

# Load .env from project root
_project_root = Path(__file__).resolve().parent.parent.parent
load_dotenv(_project_root / ".env")

from cisco_vmanage_mcp.client import VManageClient, VManageError


def _setup_logging(verbose: bool) -> None:
    """Configure logging based on verbosity."""
    level = logging.DEBUG if verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    if not verbose:
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)


def _run_async(coro):
    """Run an async coroutine synchronously."""
    return asyncio.run(coro)


def _output(data, as_json: bool, formatter):
    """Output data as JSON or formatted text."""
    if as_json:
        click.echo(json.dumps(data, indent=2, default=str))
    else:
        click.echo(formatter(data))


@click.group()
@click.option("-v", "--verbose", is_flag=True, help="Enable debug logging")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def cli(ctx, verbose: bool, as_json: bool):
    """Cisco vManage MCP - CLI companion tool."""
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose
    ctx.obj["as_json"] = as_json
    _setup_logging(verbose)


@cli.command()
@click.pass_context
def status(ctx):
    """Test connectivity to vManage, print version and device count."""
    as_json = ctx.obj["as_json"]

    async def _status():
        client = VManageClient()
        try:
            await client.authenticate()
            start = time.monotonic()
            data = await client.get("/dataservice/device")
            duration_ms = (time.monotonic() - start) * 1000
            devices = data.get("data", [])

            result = {
                "connected": True,
                "host": client.host,
                "port": client.port,
                "device_count": len(devices),
                "controllers": sum(1 for d in devices if d.get("device-type") != "vedge"),
                "edges": sum(1 for d in devices if d.get("device-type") == "vedge"),
                "response_time_ms": round(duration_ms, 1),
            }
            return result
        finally:
            await client.close()

    try:
        result = _run_async(_status())
        _output(result, as_json, _format_status)
    except VManageError as e:
        _error_exit(str(e), as_json)


def _format_status(data: dict) -> str:
    lines = [
        f"vManage Connection: OK",
        f"  Host:        {data['host']}:{data['port']}",
        f"  Devices:     {data['device_count']} total ({data['controllers']} controllers, {data['edges']} edges)",
        f"  Response:    {data['response_time_ms']}ms",
    ]
    return "\n".join(lines)


@cli.command()
@click.option("--type", "device_type", help="Filter by device type (vedge, vsmart, vbond, vmanage)")
@click.option("--reachability", help="Filter by reachability (reachable, unreachable)")
@click.option("--limit", default=50, help="Maximum devices to show")
@click.pass_context
def devices(ctx, device_type: str | None, reachability: str | None, limit: int):
    """List all devices with reachability status."""
    as_json = ctx.obj["as_json"]

    async def _devices():
        client = VManageClient()
        try:
            data = await client.get("/dataservice/device")
            devices_list = data.get("data", [])

            if device_type:
                devices_list = [d for d in devices_list if d.get("device-type", "").lower() == device_type.lower()]
            if reachability:
                devices_list = [d for d in devices_list if d.get("reachability", "").lower() == reachability.lower()]

            devices_list = devices_list[:limit]

            return [
                {
                    "hostname": d.get("host-name", "N/A"),
                    "system_ip": d.get("system-ip", "N/A"),
                    "device_type": d.get("device-type", "N/A"),
                    "device_model": d.get("device-model", "N/A"),
                    "reachability": d.get("reachability", "N/A"),
                    "site_id": d.get("site-id", "N/A"),
                    "version": d.get("version", "N/A"),
                }
                for d in devices_list
            ]
        finally:
            await client.close()

    try:
        result = _run_async(_devices())
        _output(result, as_json, _format_devices)
    except VManageError as e:
        _error_exit(str(e), as_json)


def _format_devices(devices_list: list[dict]) -> str:
    if not devices_list:
        return "No devices found."

    # Column widths
    header = f"{'Hostname':<20} {'System IP':<16} {'Type':<10} {'Model':<18} {'Status':<14} {'Site':<8} {'Version'}"
    sep = "-" * len(header)
    lines = [header, sep]
    for d in devices_list:
        status_icon = "+" if d["reachability"] == "reachable" else "-"
        lines.append(
            f"{d['hostname']:<20} {d['system_ip']:<16} {d['device_type']:<10} "
            f"{d['device_model']:<18} {status_icon} {d['reachability']:<12} {d['site_id']:<8} {d['version']}"
        )
    lines.append(f"\nTotal: {len(devices_list)} device(s)")
    return "\n".join(lines)


@cli.command()
@click.pass_context
def health(ctx):
    """Run fabric health assessment (correlation engine)."""
    as_json = ctx.obj["as_json"]

    async def _health():
        from cisco_vmanage_mcp.services.correlation import correlate_fabric_state
        client = VManageClient()
        try:
            report = await correlate_fabric_state(client)
            return report
        finally:
            await client.close()

    try:
        report = _run_async(_health())
        if as_json:
            click.echo(json.dumps(_correlation_report_to_dict(report), indent=2, default=str))
        else:
            click.echo(_format_health(report))
    except VManageError as e:
        _error_exit(str(e), as_json)


def _correlation_report_to_dict(report) -> dict:
    """Convert correlation report to a JSON-serializable dict."""
    fabric = report.fabric_report
    return {
        "overall_health": fabric.overall_health.value,
        "partial": fabric.partial,
        "incomplete_sources": fabric.incomplete_sources,
        "device_count": len(fabric.devices),
        "alarm_counts": fabric.alarm_counts,
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
            }
            for d in fabric.devices
        ],
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
        "impact": {
            "scope": report.impact.scope if report.impact else "none",
            "affected_sites": report.impact.affected_sites if report.impact else [],
            "affected_devices": report.impact.affected_devices if report.impact else [],
        },
    }


def _format_health(report) -> str:
    fabric = report.fabric_report
    health_label = fabric.overall_health.value.upper()
    controllers = [d for d in fabric.devices if d.device_type != "vedge"]
    edges = [d for d in fabric.devices if d.device_type == "vedge"]

    lines = [
        f"Fabric Health: {health_label}",
        "",
        f"  Controllers: {len(controllers)} ({sum(1 for c in controllers if c.reachable)} reachable)",
        f"  WAN Edges:   {len(edges)} ({sum(1 for e in edges if e.reachable)} reachable)",
    ]

    if fabric.alarm_counts:
        alarm_parts = [f"{v} {k}" for k, v in sorted(fabric.alarm_counts.items())]
        lines.append(f"  Alarms:      {', '.join(alarm_parts)}")

    if fabric.partial:
        lines.append(f"\n  [partial] Incomplete sources: {', '.join(fabric.incomplete_sources)}")

    if report.root_causes:
        lines.append("\nRoot-Cause Hypotheses:")
        for rc in report.root_causes:
            lines.append(f"  {rc.rank}. {rc.hypothesis} (confidence: {rc.confidence})")
            for ev in rc.supporting_evidence:
                lines.append(f"     - {ev}")
            if rc.suggested_checks:
                lines.append(f"     Checks: {'; '.join(rc.suggested_checks)}")

    if report.impact and report.impact.scope != "none":
        impact = report.impact
        lines.append(f"\nImpact: {impact.scope}")
        lines.append(f"  {impact.estimated_user_impact}")

    # Data sources
    lines.append("\nData Sources:")
    for fr in fabric.fetch_results:
        status = "OK" if fr.success else f"FAILED ({fr.error})"
        lines.append(f"  {fr.source.value}: {status} ({fr.duration_ms:.0f}ms)")

    return "\n".join(lines)


@cli.command()
@click.option("--severity", help="Filter by severity (Critical, Major, Medium, Minor)")
@click.option("--hours", "hours_back", default=24, help="Look back N hours (default: 24)")
@click.option("--limit", default=25, help="Maximum alarms to show")
@click.pass_context
def alarms(ctx, severity: str | None, hours_back: int, limit: int):
    """List active alarms with severity."""
    as_json = ctx.obj["as_json"]

    async def _alarms():
        client = VManageClient()
        try:
            data = await client.get("/dataservice/alarms")
            alarm_list = data.get("data", [])

            # Time filter
            if hours_back:
                cutoff = int(time.time() * 1000) - (hours_back * 3600 * 1000)
                alarm_list = [
                    a for a in alarm_list
                    if int(a.get("entry_time", 0) or 0) >= cutoff
                ]

            if severity:
                alarm_list = [
                    a for a in alarm_list
                    if a.get("severity", "").lower() == severity.lower()
                ]

            # Sort: Critical first, then by time
            severity_order = {"critical": 0, "major": 1, "medium": 2, "minor": 3}
            alarm_list.sort(key=lambda a: (
                severity_order.get(a.get("severity", "").lower(), 9),
                -int(a.get("entry_time", 0) or 0),
            ))

            alarm_list = alarm_list[:limit]

            return [
                {
                    "severity": a.get("severity", "N/A"),
                    "type": a.get("type", a.get("rule_name_display", "N/A")),
                    "hostname": a.get("host_name", a.get("system_ip", "N/A")),
                    "system_ip": a.get("system_ip", "N/A"),
                    "message": a.get("message", ""),
                    "time": a.get("entry_time", ""),
                }
                for a in alarm_list
            ]
        finally:
            await client.close()

    try:
        result = _run_async(_alarms())
        _output(result, as_json, _format_alarms)
    except VManageError as e:
        _error_exit(str(e), as_json)


def _format_alarms(alarm_list: list[dict]) -> str:
    if not alarm_list:
        return "No active alarms."

    lines = [f"{'Severity':<10} {'Type':<30} {'Device':<20} {'Message'}"]
    lines.append("-" * 90)
    for a in alarm_list:
        sev = a["severity"]
        # Severity indicator
        indicator = {"Critical": "!!", "Major": "! ", "Medium": "~ ", "Minor": "  "}.get(sev, "  ")
        msg = a["message"][:50] if a["message"] else ""
        lines.append(
            f"{indicator}{sev:<8} {a['type']:<30} {a['hostname']:<20} {msg}"
        )
    lines.append(f"\nTotal: {len(alarm_list)} alarm(s)")
    return "\n".join(lines)


@cli.command()
@click.argument("system_ip")
@click.pass_context
def diagnose(ctx, system_ip: str):
    """Run device diagnosis for a system IP."""
    as_json = ctx.obj["as_json"]

    async def _diagnose():
        from cisco_vmanage_mcp.services.correlation import diagnose_device
        client = VManageClient()
        try:
            report = await diagnose_device(client, system_ip)
            return report
        finally:
            await client.close()

    try:
        report = _run_async(_diagnose())
        if as_json:
            click.echo(json.dumps(_diagnosis_to_dict(report), indent=2, default=str))
        else:
            click.echo(_format_diagnosis(report))
    except VManageError as e:
        _error_exit(str(e), as_json)


def _diagnosis_to_dict(report) -> dict:
    """Convert diagnosis report to JSON-serializable dict."""
    result: dict = {
        "system_ip": report.system_ip if hasattr(report, "system_ip") else "unknown",
    }
    if hasattr(report, "device") and report.device:
        d = report.device
        result["device"] = {
            "hostname": d.hostname,
            "system_ip": d.system_ip,
            "site_id": d.site_id,
            "reachable": d.reachable,
            "health": d.overall_health.value,
            "bfd_sessions": d.bfd_sessions,
            "control_connections": d.control_connections,
        }
    if hasattr(report, "fabric_context") and report.fabric_context:
        result["fabric_health"] = report.fabric_context.overall_health.value
    if hasattr(report, "is_isolated"):
        result["is_isolated"] = report.is_isolated
    if hasattr(report, "root_causes"):
        result["root_causes"] = [
            {"hypothesis": rc.hypothesis, "confidence": rc.confidence}
            for rc in report.root_causes
        ]
    if hasattr(report, "narrative"):
        result["narrative"] = report.narrative
    return result


def _format_diagnosis(report) -> str:
    lines = []
    if hasattr(report, "device") and report.device:
        d = report.device
        lines.append(f"Device Diagnosis: {d.hostname} ({d.system_ip})")
        lines.append(f"  Site:             {d.site_id}")
        lines.append(f"  Reachable:        {'Yes' if d.reachable else 'No'}")
        lines.append(f"  Health:           {d.overall_health.value.upper()}")
        lines.append(f"  BFD Sessions:     {d.bfd_sessions}")
        lines.append(f"  Control Conns:    {d.control_connections}")
    elif hasattr(report, "narrative") and report.narrative:
        lines.append(report.narrative)
    else:
        lines.append("No device data found.")

    if hasattr(report, "is_isolated"):
        scope = "isolated" if report.is_isolated else "part of wider failure"
        lines.append(f"  Failure Scope:    {scope}")

    if hasattr(report, "root_causes") and report.root_causes:
        lines.append("\nRoot-Cause Hypotheses:")
        for rc in report.root_causes:
            lines.append(f"  {rc.rank}. {rc.hypothesis} (confidence: {rc.confidence})")

    if hasattr(report, "narrative") and report.narrative and hasattr(report, "device") and report.device:
        lines.append(f"\n{report.narrative}")

    return "\n".join(lines)


@cli.command(name="smoke-test")
@click.pass_context
def smoke_test(ctx):
    """Run a quick connectivity and auth check, exit 0 if healthy."""
    as_json = ctx.obj["as_json"]

    async def _smoke():
        client = VManageClient()
        checks: list[dict] = []
        try:
            # Check 1: Authentication
            start = time.monotonic()
            await client.authenticate()
            auth_ms = (time.monotonic() - start) * 1000
            checks.append({"check": "authentication", "status": "pass", "duration_ms": round(auth_ms, 1)})

            # Check 2: Device list
            start = time.monotonic()
            data = await client.get("/dataservice/device")
            api_ms = (time.monotonic() - start) * 1000
            device_count = len(data.get("data", []))
            checks.append({
                "check": "device_api",
                "status": "pass",
                "duration_ms": round(api_ms, 1),
                "device_count": device_count,
            })

            # Check 3: Alarm API
            start = time.monotonic()
            await client.get("/dataservice/alarms/count")
            alarm_ms = (time.monotonic() - start) * 1000
            checks.append({"check": "alarm_api", "status": "pass", "duration_ms": round(alarm_ms, 1)})

            return {"overall": "pass", "checks": checks}
        except Exception as e:
            checks.append({"check": "failed", "status": "fail", "error": str(e)})
            return {"overall": "fail", "checks": checks}
        finally:
            await client.close()

    result = _run_async(_smoke())

    if as_json:
        click.echo(json.dumps(result, indent=2))
    else:
        overall = result["overall"]
        click.echo(f"Smoke Test: {'PASS' if overall == 'pass' else 'FAIL'}")
        for check in result["checks"]:
            icon = "+" if check["status"] == "pass" else "x"
            duration = f" ({check['duration_ms']}ms)" if "duration_ms" in check else ""
            extra = f" [{check['device_count']} devices]" if "device_count" in check else ""
            error = f" - {check['error']}" if "error" in check else ""
            click.echo(f"  [{icon}] {check['check']}{duration}{extra}{error}")

    sys.exit(0 if result["overall"] == "pass" else 1)


def _error_exit(message: str, as_json: bool) -> None:
    """Print error and exit with code 1."""
    if as_json:
        click.echo(json.dumps({"error": message}))
    else:
        click.echo(f"Error: {message}", err=True)
    sys.exit(1)


if __name__ == "__main__":
    cli()
