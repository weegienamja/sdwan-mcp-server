"""Network state correlation and root-cause analysis.

This module implements the core intelligence layer:
- Correlates unreachable edges with missing control connections
- Maps BFD failures to likely transport issues
- Identifies isolated vs widespread failures
- Estimates blast radius by site/region
- Generates ordered root-cause hypotheses

All reasoning is computed in Python -- the LLM only explains results.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from cisco_vmanage_mcp.client import VManageClient
from cisco_vmanage_mcp.services.health_check import (
    DeviceHealth,
    FabricHealthReport,
    HealthLevel,
    assess_fabric_health,
)


@dataclass
class SiteStatus:
    """Aggregated status for a single site."""
    site_id: str
    devices: list[DeviceHealth] = field(default_factory=list)
    all_unreachable: bool = False
    partially_degraded: bool = False

    @property
    def device_count(self) -> int:
        return len(self.devices)

    @property
    def unreachable_count(self) -> int:
        return sum(1 for d in self.devices if not d.reachable)

    @property
    def reachable_count(self) -> int:
        return sum(1 for d in self.devices if d.reachable)


@dataclass
class RootCauseHypothesis:
    """A ranked root-cause hypothesis for a detected issue."""
    rank: int
    hypothesis: str
    confidence: str  # "high", "medium", "low"
    supporting_evidence: list[str] = field(default_factory=list)
    suggested_checks: list[str] = field(default_factory=list)


@dataclass
class ImpactAssessment:
    """Blast radius and impact analysis for a failure."""
    scope: str  # "device", "site", "transport", "fabric-wide"
    affected_sites: list[str] = field(default_factory=list)
    affected_devices: list[str] = field(default_factory=list)
    total_affected_bfd: int = 0
    estimated_user_impact: str = ""


@dataclass
class CorrelationReport:
    """Complete correlation and root-cause analysis."""
    fabric_report: FabricHealthReport
    site_statuses: dict[str, SiteStatus] = field(default_factory=dict)
    root_causes: list[RootCauseHypothesis] = field(default_factory=list)
    impact: ImpactAssessment | None = None
    narrative: str = ""
    data_sources_used: list[str] = field(default_factory=list)


def _group_by_site(devices: list[DeviceHealth]) -> dict[str, SiteStatus]:
    """Group devices by site ID and compute per-site status."""
    sites: dict[str, SiteStatus] = {}
    for d in devices:
        sid = d.site_id
        if sid not in sites:
            sites[sid] = SiteStatus(site_id=sid)
        sites[sid].devices.append(d)

    for site in sites.values():
        site.all_unreachable = all(not d.reachable for d in site.devices)
        site.partially_degraded = any(
            d.overall_health in (HealthLevel.DEGRADED, HealthLevel.CRITICAL)
            for d in site.devices
        )

    return sites


def _analyze_failure_scope(
    devices: list[DeviceHealth],
    sites: dict[str, SiteStatus],
) -> ImpactAssessment:
    """Determine whether failure is isolated, site-level, transport-level, or fabric-wide."""
    unreachable = [d for d in devices if not d.reachable]

    if not unreachable:
        return ImpactAssessment(
            scope="none",
            estimated_user_impact="No unreachable devices detected.",
        )

    unreachable_sites = list({d.site_id for d in unreachable})
    total_sites = list(sites.keys())

    # Check if controllers are down
    unreachable_controllers = [d for d in unreachable if d.device_type != "vedge"]
    if unreachable_controllers:
        return ImpactAssessment(
            scope="fabric-wide",
            affected_sites=total_sites,
            affected_devices=[d.hostname for d in unreachable],
            total_affected_bfd=sum(d.bfd_sessions for d in unreachable),
            estimated_user_impact=(
                f"Controller(s) unreachable ({', '.join(d.hostname for d in unreachable_controllers)}). "
                f"This may affect policy distribution and route updates fabric-wide."
            ),
        )

    # Check if all sites have issues (fabric-wide transport)
    if len(unreachable_sites) > len(total_sites) * 0.5:
        return ImpactAssessment(
            scope="fabric-wide",
            affected_sites=unreachable_sites,
            affected_devices=[d.hostname for d in unreachable],
            total_affected_bfd=sum(d.bfd_sessions for d in unreachable),
            estimated_user_impact=(
                f"Multiple sites affected ({len(unreachable_sites)}/{len(total_sites)}). "
                f"This suggests a widespread transport or control-plane issue."
            ),
        )

    # Single site with all devices down
    fully_down_sites = [s for s in sites.values() if s.all_unreachable and s.device_count > 0]
    if fully_down_sites and len(unreachable_sites) == 1:
        site = fully_down_sites[0]
        return ImpactAssessment(
            scope="site",
            affected_sites=[site.site_id],
            affected_devices=[d.hostname for d in site.devices],
            total_affected_bfd=sum(d.bfd_sessions for d in site.devices),
            estimated_user_impact=(
                f"All devices at site {site.site_id} are unreachable. "
                f"Impact is isolated to this site. Other sites maintain connectivity."
            ),
        )

    # Individual device(s) down
    return ImpactAssessment(
        scope="device",
        affected_sites=unreachable_sites,
        affected_devices=[d.hostname for d in unreachable],
        total_affected_bfd=sum(d.bfd_sessions for d in unreachable),
        estimated_user_impact=(
            f"{len(unreachable)} device(s) unreachable across {len(unreachable_sites)} site(s). "
            f"Other WAN edges at the same site(s) maintain connectivity, "
            f"suggesting device-specific rather than site-level failure."
        ),
    )


def _device_scope_causes(
    devices: list[DeviceHealth],
    unreachable: list[DeviceHealth],
) -> list[RootCauseHypothesis]:
    causes: list[RootCauseHypothesis] = []
    for device in unreachable:
        if device.bfd_sessions == 0 and device.control_connections == 0:
            causes.append(RootCauseHypothesis(
                rank=len(causes) + 1,
                hypothesis=(
                    f"{device.hostname} is completely isolated (0 BFD, 0 control connections)"
                ),
                confidence="high",
                supporting_evidence=[
                    "Device reports 0 BFD sessions and 0 control connections",
                    "Other devices at sites remain healthy" if len(unreachable) == 1 else "",
                ],
                suggested_checks=[
                    f"Check physical WAN connectivity at site {device.site_id}",
                    "Verify device power state and hardware health",
                    "Check transport-facing circuit status with provider",
                    f"Review recent config changes on {device.hostname}",
                ],
            ))
    wan_edges_with_bfd = [device for device in devices if device.device_type == "vedge" and device.reachable]
    if wan_edges_with_bfd:
        causes.append(RootCauseHypothesis(
            rank=len(causes) + 1,
            hypothesis="Failure is device-specific, not transport-wide",
            confidence="medium",
            supporting_evidence=[
                f"{len(wan_edges_with_bfd)} other WAN edge(s) maintain full BFD mesh",
                "If transport were down, multiple devices would be affected",
            ],
            suggested_checks=[
                "Compare tunnel stats between healthy and unhealthy devices",
                "Check for interface errors on the affected device(s)",
            ],
        ))
    return causes


def _site_scope_causes(sites: dict[str, SiteStatus]) -> list[RootCauseHypothesis]:
    return [
        RootCauseHypothesis(
            rank=rank,
            hypothesis=f"Site {site_id} WAN outage (all {site.device_count} device(s) unreachable)",
            confidence="high",
            supporting_evidence=[
                f"All devices at site {site_id} are unreachable simultaneously",
                "Other sites maintain connectivity",
            ],
            suggested_checks=[
                f"Check WAN circuit(s) at site {site_id}",
                "Verify upstream router/switch at the site",
                f"Contact transport provider for site {site_id} circuit status",
            ],
        )
        for rank, (site_id, site) in enumerate(
            ((site_id, site) for site_id, site in sites.items() if site.all_unreachable),
            start=1,
        )
    ]


def _fabric_scope_causes(unreachable: list[DeviceHealth]) -> list[RootCauseHypothesis]:
    unreachable_controllers = [device for device in unreachable if device.device_type != "vedge"]
    if unreachable_controllers:
        return [RootCauseHypothesis(
            rank=1,
            hypothesis="Controller failure causing fabric-wide impact",
            confidence="high",
            supporting_evidence=[
                f"Controller(s) unreachable: "
                f"{', '.join(device.hostname for device in unreachable_controllers)}",
                "Controller loss prevents policy/route distribution",
            ],
            suggested_checks=[
                "Check controller infrastructure (vManage/vSmart/vBond)",
                "Verify data centre connectivity",
                "Review controller resource usage (CPU, memory, disk)",
            ],
        )]
    return [RootCauseHypothesis(
        rank=1,
        hypothesis="Widespread transport failure affecting multiple sites",
        confidence="medium",
        supporting_evidence=[
            f"{len(unreachable)} device(s) unreachable across multiple sites",
            "Controllers are reachable, ruling out control-plane origin",
        ],
        suggested_checks=[
            "Check common transport provider circuits",
            "Verify backbone/core connectivity",
            "Look for correlated alarms across affected sites",
        ],
    )]


def _generate_root_causes(
    devices: list[DeviceHealth],
    sites: dict[str, SiteStatus],
    impact: ImpactAssessment,
) -> list[RootCauseHypothesis]:
    """Generate ordered root-cause hypotheses based on correlation signals."""
    unreachable = [device for device in devices if not device.reachable]
    if not unreachable:
        return []
    if impact.scope == "device":
        return _device_scope_causes(devices, unreachable)
    if impact.scope == "site":
        return _site_scope_causes(sites)
    if impact.scope == "fabric-wide":
        return _fabric_scope_causes(unreachable)
    return []


def _build_narrative(
    report: FabricHealthReport,
    sites: dict[str, SiteStatus],
    impact: ImpactAssessment,
    root_causes: list[RootCauseHypothesis],
) -> str:
    """Build a structured narrative from computed signals -- not LLM-generated."""
    lines: list[str] = []

    # Completeness caveat
    if report.partial:
        lines.append(
            f"**Note:** This assessment is partial. "
            f"Data from {', '.join(report.incomplete_sources)} could not be retrieved, "
            f"so conclusions dependent on those sources may be incomplete."
        )
        lines.append("")

    # Overall status
    wan_edges = [d for d in report.devices if d.device_type == "vedge"]
    controllers = [d for d in report.devices if d.device_type != "vedge"]

    lines.append(f"## Fabric Health: {report.overall_health.value.upper()}")
    lines.append("")

    # Device summary
    lines.append("### Devices")
    lines.append(
        f"- Controllers: {len(controllers)} "
        f"({sum(1 for c in controllers if c.reachable)} reachable)"
    )
    lines.append(
        f"- WAN Edges: {len(wan_edges)} "
        f"({sum(1 for w in wan_edges if w.reachable)} reachable, "
        f"{sum(1 for w in wan_edges if not w.reachable)} unreachable)"
    )

    # Alarm summary
    if report.alarm_counts:
        lines.append("")
        lines.append("### Alarms")
        for sev in ["Critical", "Major", "Medium", "Minor"]:
            count = report.alarm_counts.get(sev, 0)
            if count > 0:
                lines.append(f"- {sev}: {count}")

    # Impact
    if impact and impact.scope != "none":
        lines.append("")
        lines.append("### Impact Assessment")
        lines.append(f"- Scope: **{impact.scope}**")
        lines.append(f"- {impact.estimated_user_impact}")
        if impact.affected_devices:
            lines.append(f"- Affected: {', '.join(impact.affected_devices)}")

    # Root causes
    if root_causes:
        lines.append("")
        lines.append("### Root-Cause Hypotheses")
        for rc in root_causes:
            lines.append(f"**{rc.rank}. {rc.hypothesis}** (confidence: {rc.confidence})")
            for ev in rc.supporting_evidence:
                if ev:
                    lines.append(f"   - {ev}")
            if rc.suggested_checks:
                lines.append(f"   - Suggested checks: {'; '.join(rc.suggested_checks)}")

    # Data sources
    lines.append("")
    lines.append("### Data Sources")
    for fr in report.fetch_results:
        status = "OK" if fr.success else f"FAILED ({fr.error})"
        lines.append(f"- {fr.source.value}: {status} ({fr.duration_ms:.0f}ms)")

    return "\n".join(lines)


async def correlate_fabric_state(client: VManageClient) -> CorrelationReport:
    """Full correlation analysis: fetch data, compute health, correlate, explain.

    This is the primary entry point for intelligent fabric analysis.
    """
    # Step 1: Assess fabric health (fetches data + computes per-device signals)
    fabric_report = await assess_fabric_health(client)

    # Step 2: Group by site
    site_statuses = _group_by_site(fabric_report.devices)

    # Step 3: Assess impact / blast radius
    impact = _analyze_failure_scope(fabric_report.devices, site_statuses)

    # Step 4: Generate root-cause hypotheses
    root_causes = _generate_root_causes(
        fabric_report.devices, site_statuses, impact
    )

    # Step 5: Build narrative
    narrative = _build_narrative(fabric_report, site_statuses, impact, root_causes)

    # Step 6: Track data sources
    data_sources_used = [
        fr.source.value for fr in fabric_report.fetch_results if fr.success
    ]

    return CorrelationReport(
        fabric_report=fabric_report,
        site_statuses=site_statuses,
        root_causes=root_causes,
        impact=impact,
        narrative=narrative,
        data_sources_used=data_sources_used,
    )


async def diagnose_device(
    client: VManageClient,
    system_ip: str,
) -> tuple[CorrelationReport, DeviceHealth | None]:
    """Diagnose a specific device with full fabric context.

    Fetches both fabric-wide and device-specific data to provide
    context-aware diagnosis (e.g., "is this device isolated, or is
    the whole site down?").
    """
    from cisco_vmanage_mcp.services.health_check import assess_device_health

    # Fetch fabric-wide context and device-specific data concurrently
    fabric_report, (device_health, device_fetches) = await asyncio.gather(
        assess_fabric_health(client),
        assess_device_health(client, system_ip),
    )

    # Merge device-specific fetch results
    fabric_report.fetch_results.extend(device_fetches)

    # Build correlation with device focus
    site_statuses = _group_by_site(fabric_report.devices)
    impact = _analyze_failure_scope(fabric_report.devices, site_statuses)
    root_causes = _generate_root_causes(fabric_report.devices, site_statuses, impact)
    narrative = _build_narrative(fabric_report, site_statuses, impact, root_causes)

    # Add device-specific section
    if device_health:
        device_lines = ["\n### Device-Specific Analysis"]
        device_lines.append(
            f"**{device_health.hostname}** ({device_health.system_ip}) "
            f"at site {device_health.site_id}"
        )
        device_lines.append(f"- Health: **{device_health.overall_health.value}**")
        device_lines.append(f"- BFD Sessions: {device_health.bfd_sessions}")
        device_lines.append(f"- Control Connections: {device_health.control_connections}")

        if device_health.signals:
            device_lines.append("\n**Signals:**")
            for sig in device_health.signals:
                icon = {"critical": "[!]", "degraded": "[~]", "healthy": "[+]", "unknown": "[?]"}
                device_lines.append(
                    f"- {icon.get(sig.level.value, '[?]')} {sig.summary} "
                    f"(source: {sig.source.value})"
                )
        else:
            device_lines.append("- No issues detected on this device.")

        narrative += "\n" + "\n".join(device_lines)

    data_sources_used = [
        fr.source.value for fr in fabric_report.fetch_results if fr.success
    ]

    correlation = CorrelationReport(
        fabric_report=fabric_report,
        site_statuses=site_statuses,
        root_causes=root_causes,
        impact=impact,
        narrative=narrative,
        data_sources_used=data_sources_used,
    )

    return correlation, device_health
