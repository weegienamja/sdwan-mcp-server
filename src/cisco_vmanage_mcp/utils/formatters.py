"""Markdown and JSON response formatters."""

from datetime import UTC, datetime


def _ms_to_readable(ms_timestamp) -> str:
    """Convert millisecond unix timestamp to human-readable string."""
    try:
        ts = int(ms_timestamp) / 1000
        dt = datetime.fromtimestamp(ts, tz=UTC)
        return dt.strftime("%d %b %Y %H:%M UTC")
    except (ValueError, TypeError, OSError):
        return "N/A"


def _safe_str(value, default="--") -> str:
    """Safely convert a value to string, handling '--' and None."""
    if value is None:
        return default
    return str(value)


def format_device_table_markdown(devices: list, total: int, offset: int) -> str:
    """Format device list as a markdown table."""
    if not devices:
        return "No devices found matching the specified filters."

    lines = [
        f"**SD-WAN Device Inventory** ({total} total)\n",
        "| Hostname | System IP | Model | Reachability | Version | Site ID | BFD | Control |",
        "|----------|-----------|-------|--------------|---------|---------|-----|---------|",
    ]

    for d in devices:
        reachability = d.get("reachability", "N/A")
        indicator = "+" if reachability.lower() == "reachable" else "X"

        lines.append(
            f"| {d.get('host-name', 'N/A')} "
            f"| {d.get('system-ip', 'N/A')} "
            f"| {d.get('device-model', 'N/A')} "
            f"| {indicator} {reachability} "
            f"| {d.get('version', 'N/A')} "
            f"| {d.get('site-id', 'N/A')} "
            f"| {_safe_str(d.get('bfdSessions'))} "
            f"| {_safe_str(d.get('controlConnections'))} |"
        )

    if total > offset + len(devices):
        lines.append(
            f"\n*Showing {offset + 1}-{offset + len(devices)} of {total}. "
            f"Use offset={offset + len(devices)} to see more.*"
        )

    return "\n".join(lines)


def format_device_status_markdown(d: dict) -> str:
    """Format a single device's detailed status."""
    lines = [
        f"**Device Status: {d.get('host-name', 'N/A')}**\n",
        "| Field | Value |",
        "|-------|-------|",
        f"| Hostname | {d.get('host-name', 'N/A')} |",
        f"| System IP | {d.get('system-ip', 'N/A')} |",
        f"| Device Type | {d.get('device-type', 'N/A')} |",
        f"| Device Model | {d.get('device-model', 'N/A')} |",
        f"| Version | {d.get('version', 'N/A')} |",
        f"| Reachability | {d.get('reachability', 'N/A')} |",
        f"| State | {d.get('state', 'N/A')} ({d.get('state_description', 'N/A')}) |",
        f"| Site ID | {d.get('site-id', 'N/A')} |",
        f"| UUID | {d.get('uuid', 'N/A')} |",
        f"| Board Serial | {d.get('board-serial', 'N/A')} |",
        f"| Certificate | {d.get('certificate-validity', 'N/A')} |",
        f"| Uptime Since | {_ms_to_readable(d.get('uptime-date'))} |",
        f"| CPU Count | {_safe_str(d.get('total_cpu_count'))} |",
        f"| BFD Sessions | {_safe_str(d.get('bfdSessions'))} |",
        f"| Control Connections | {_safe_str(d.get('controlConnections'))} |",
        f"| Connected vManages | {', '.join(d.get('connectedVManages', []))} |",
        f"| Location | {_safe_str(d.get('latitude'))}, {_safe_str(d.get('longitude'))} |",
    ]
    return "\n".join(lines)


def format_interfaces_markdown(interfaces: list, system_ip: str) -> str:
    """Format device interfaces as markdown table."""
    if not interfaces:
        return f"No interfaces found for device {system_ip}."

    lines = [
        f"**Interfaces for {system_ip}** ({len(interfaces)} total)\n",
        "| Interface | Status | IP Address | Speed | TX Octets | RX Octets |",
        "|-----------|--------|------------|-------|-----------|-----------|",
    ]

    for i in interfaces:
        lines.append(
            f"| {_safe_str(i.get('ifname'))} "
            f"| {_safe_str(i.get('if-admin-status'))} / {_safe_str(i.get('if-oper-status'))} "
            f"| {_safe_str(i.get('ip-address', 'N/A'))} "
            f"| {_safe_str(i.get('speed-mbps', i.get('if-speed')))} "
            f"| {_safe_str(i.get('tx-octets'))} "
            f"| {_safe_str(i.get('rx-octets'))} |"
        )

    return "\n".join(lines)


def format_counters_markdown(counters: list, system_ip: str) -> str:
    """Format device counters as markdown."""
    if not counters:
        return f"No counters found for device {system_ip}."

    lines = [f"**Device Counters for {system_ip}**\n"]

    for c in counters:
        lines.append("| Field | Value |")
        lines.append("|-------|-------|")
        for key, value in c.items():
            if key not in ("vdevice-dataKey", "vdevice-host-name", "vdevice-name"):
                lines.append(f"| {key} | {_safe_str(value)} |")

    return "\n".join(lines)


def format_alarm_markdown(alarms: list, total: int) -> str:
    """Format alarms as a markdown list with severity indicators."""
    if not alarms:
        return "No active alarms found."

    severity_emoji = {
        "Critical": "[CRIT]",
        "Major": "[MAJR]",
        "Medium": "[MED]",
        "Minor": "[MIN]",
    }

    lines = [f"**Active Alarms** ({total} total)\n"]

    for a in alarms:
        severity = a.get("severity", "Unknown")
        tag = severity_emoji.get(severity, "[???]")
        lines.append(
            f"- {tag} **{severity}** | {a.get('type', 'N/A')} | "
            f"{a.get('host-name', 'N/A')} ({a.get('system-ip', 'N/A')}) | "
            f"{a.get('active-time', 'N/A')}"
        )

    return "\n".join(lines)


def format_alarm_count_markdown(counts: dict) -> str:
    """Format alarm counts as markdown."""
    lines = ["**Alarm Counts**\n"]
    for severity, count in counts.items():
        lines.append(f"- {severity}: {count}")
    return "\n".join(lines)


def format_events_markdown(events: list, total: int) -> str:
    """Format events as a markdown list."""
    if not events:
        return "No events found in the specified time range."

    lines = [f"**System Events** ({total} total)\n"]

    for e in events:
        lines.append(
            f"- [{_safe_str(e.get('severity', e.get('severity_level', 'Info')))}] "
            f"{_safe_str(e.get('eventname', e.get('type', 'N/A')))} | "
            f"{_safe_str(e.get('host-name', e.get('host_name', 'N/A')))} "
            f"({_safe_str(e.get('system-ip', e.get('system_ip', 'N/A')))}) | "
            f"{_safe_str(e.get('entry_time', e.get('receive_time', 'N/A')))}"
        )

    return "\n".join(lines)


def format_tunnel_table_markdown(tunnels: list, system_ip: str) -> str:
    """Format tunnel list as markdown table."""
    if not tunnels:
        return f"No tunnels found for device {system_ip}."

    lines = [
        f"**Tunnels for {system_ip}** ({len(tunnels)} total)\n",
        "| Dest IP | Remote System IP | Protocol | Local Color | Remote Color | TX Pkts | RX Pkts |",
        "|---------|-----------------|----------|-------------|--------------|---------|---------|",
    ]

    for t in tunnels:
        lines.append(
            f"| {_safe_str(t.get('dest-ip'))} "
            f"| {_safe_str(t.get('system-ip'))} "
            f"| {_safe_str(t.get('tunnel-protocol'))} "
            f"| {_safe_str(t.get('local-color'))} "
            f"| {_safe_str(t.get('remote-color'))} "
            f"| {_safe_str(t.get('tx_pkts'))} "
            f"| {_safe_str(t.get('rx_pkts'))} |"
        )

    return "\n".join(lines)


def format_bfd_table_markdown(sessions: list, system_ip: str) -> str:
    """Format BFD sessions as markdown table."""
    if not sessions:
        return f"No BFD sessions found for device {system_ip}."

    lines = [
        f"**BFD Sessions for {system_ip}** ({len(sessions)} total)\n",
        "| Peer System IP | State | Source TLOC | Dest TLOC | Site ID |",
        "|----------------|-------|-------------|-----------|---------|",
    ]

    for s in sessions:
        lines.append(
            f"| {_safe_str(s.get('system-ip'))} "
            f"| {_safe_str(s.get('state'))} "
            f"| {_safe_str(s.get('src-ip'))}:{_safe_str(s.get('src-port'))} "
            f"| {_safe_str(s.get('dst-ip'))}:{_safe_str(s.get('dst-port'))} "
            f"| {_safe_str(s.get('site-id'))} |"
        )

    return "\n".join(lines)


def format_omp_peers_markdown(peers: list, system_ip: str) -> str:
    """Format OMP peers as markdown table."""
    if not peers:
        return f"No OMP peers found for device {system_ip}."

    lines = [
        f"**OMP Peers for {system_ip}** ({len(peers)} total)\n",
        "| Peer | State | Site ID | Domain ID | Type |",
        "|------|-------|---------|-----------|------|",
    ]

    for p in peers:
        lines.append(
            f"| {_safe_str(p.get('peer'))} "
            f"| {_safe_str(p.get('state'))} "
            f"| {_safe_str(p.get('site-id'))} "
            f"| {_safe_str(p.get('domain-id'))} "
            f"| {_safe_str(p.get('type'))} |"
        )

    return "\n".join(lines)


def format_control_connections_markdown(connections: list, system_ip: str) -> str:
    """Format control connections as markdown table."""
    if not connections:
        return f"No control connections found for device {system_ip}."

    lines = [
        f"**Control Connections for {system_ip}** ({len(connections)} total)\n",
        "| Peer Type | Peer System IP | State | Uptime |",
        "|-----------|----------------|-------|--------|",
    ]

    for c in connections:
        lines.append(
            f"| {_safe_str(c.get('peer-type'))} "
            f"| {_safe_str(c.get('system-ip'))} "
            f"| {_safe_str(c.get('state'))} "
            f"| {_safe_str(c.get('uptime'))} |"
        )

    return "\n".join(lines)


def format_policies_markdown(policies: list, total: int, offset: int) -> str:
    """Format policies as markdown table."""
    if not policies:
        return "No vSmart policies found."

    lines = [
        f"**vSmart Policies** ({total} total)\n",
        "| Name | Description | Type | Active |",
        "|------|-------------|------|--------|",
    ]

    for p in policies:
        active = "Yes" if p.get("isPolicyActivated") else "No"
        lines.append(
            f"| {_safe_str(p.get('policyName'))} "
            f"| {_safe_str(p.get('policyDescription', 'N/A'))} "
            f"| {_safe_str(p.get('policyType'))} "
            f"| {active} |"
        )

    if total > offset + len(policies):
        lines.append(
            f"\n*Showing {offset + 1}-{offset + len(policies)} of {total}. "
            f"Use offset={offset + len(policies)} to see more.*"
        )

    return "\n".join(lines)


def format_templates_markdown(templates: list, total: int, offset: int) -> str:
    """Format device templates as markdown table."""
    if not templates:
        return "No device templates found."

    lines = [
        f"**Device Templates** ({total} total)\n",
        "| Name | Description | Device Type | Attached Devices |",
        "|------|-------------|-------------|------------------|",
    ]

    for t in templates:
        lines.append(
            f"| {_safe_str(t.get('templateName'))} "
            f"| {_safe_str(t.get('templateDescription', 'N/A'))} "
            f"| {_safe_str(t.get('deviceType'))} "
            f"| {_safe_str(t.get('devicesAttached'))} |"
        )

    if total > offset + len(templates):
        lines.append(
            f"\n*Showing {offset + 1}-{offset + len(templates)} of {total}. "
            f"Use offset={offset + len(templates)} to see more.*"
        )

    return "\n".join(lines)


def format_system_status_markdown(statuses: list, system_ip: str) -> str:
    """Format system status as markdown."""
    if not statuses:
        return f"No system status data found for device {system_ip}."

    lines = [f"**System Status for {system_ip}**\n"]

    for s in statuses:
        lines.append("| Field | Value |")
        lines.append("|-------|-------|")
        lines.append(f"| CPU Load (1min) | {_safe_str(s.get('min1_avg'))} |")
        lines.append(f"| CPU Load (5min) | {_safe_str(s.get('min5_avg'))} |")
        lines.append(f"| CPU Load (15min) | {_safe_str(s.get('min15_avg'))} |")
        lines.append(f"| Memory Used | {_safe_str(s.get('mem_used'))} |")
        lines.append(f"| Memory Free | {_safe_str(s.get('mem_free'))} |")
        lines.append(f"| Disk Used | {_safe_str(s.get('disk_used'))} |")
        lines.append(f"| Disk Available | {_safe_str(s.get('disk_avail'))} |")
        lines.append(f"| Uptime | {_safe_str(s.get('uptime'))} |")

    return "\n".join(lines)


def format_fabric_summary_markdown(summary: dict) -> str:
    """Format fabric summary as structured markdown."""
    lines = [
        "**SD-WAN Fabric Summary**\n",
        "## Devices",
        f"- Controllers: {summary.get('controllers', 0)} (vManage: {summary.get('vmanage_count', 0)}, vSmart: {summary.get('vsmart_count', 0)}, vBond: {summary.get('vbond_count', 0)})",
        f"- WAN Edges: {summary.get('wan_edges', 0)} ({summary.get('wan_edges_reachable', 0)} reachable, {summary.get('wan_edges_unreachable', 0)} unreachable)",
        f"- Device States: {summary.get('state_green', 0)} green, {summary.get('state_yellow', 0)} yellow, {summary.get('state_red', 0)} red",
        "",
        "## Alarms",
        f"- Critical: {summary.get('alarms_critical', 0)}",
        f"- Major: {summary.get('alarms_major', 0)}",
        f"- Medium: {summary.get('alarms_medium', 0)}",
        f"- Minor: {summary.get('alarms_minor', 0)}",
        "",
        "## Connectivity",
        f"- Total BFD Sessions: {summary.get('total_bfd_sessions', 0)}",
    ]

    unreachable = summary.get("unreachable_devices", [])
    if unreachable:
        lines.append("")
        lines.append("## Unreachable Devices")
        for d in unreachable:
            lines.append(f"- {d['hostname']} ({d['system_ip']}) -- site {d['site_id']}")

    return "\n".join(lines)
