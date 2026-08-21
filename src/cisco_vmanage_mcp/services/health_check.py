"""Computed health signals from raw vManage API data.

This module separates data collection from health assessment.
The LLM only explains computed signals -- it does not improvise conclusions.
Every health signal is traceable to a specific API source.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from cisco_vmanage_mcp.client import VManageClient


class HealthLevel(str, Enum):
    """Computed health status for a component."""
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    CRITICAL = "critical"
    UNKNOWN = "unknown"


class DataSource(str, Enum):
    """API endpoints used as evidence for health signals."""
    DEVICE_LIST = "GET /dataservice/device"
    ALARM_COUNT = "GET /dataservice/alarms/count"
    ALARMS = "GET /dataservice/alarms"
    BFD_SESSIONS = "GET /dataservice/device/bfd/sessions"
    CONTROL_CONNECTIONS = "GET /dataservice/device/control/connections"
    TUNNEL_STATS = "GET /dataservice/device/tunnel/statistics"
    SYSTEM_STATUS = "GET /dataservice/device/system/status"
    OMP_PEERS = "GET /dataservice/device/omp/peers"


@dataclass
class DataFetchResult:
    """Result of a single API fetch, tracking success/failure for partial reporting."""
    source: DataSource
    success: bool
    data: dict | list | None = None
    error: str | None = None
    duration_ms: float = 0.0


@dataclass
class HealthSignal:
    """A single computed health observation, grounded in API data."""
    level: HealthLevel
    component: str
    summary: str
    detail: str
    source: DataSource
    evidence: dict = field(default_factory=dict)


@dataclass
class DeviceHealth:
    """Computed health for a single device."""
    hostname: str
    system_ip: str
    site_id: str
    device_type: str
    device_model: str
    reachable: bool
    bfd_sessions: int
    control_connections: int
    state: str
    signals: list[HealthSignal] = field(default_factory=list)

    @property
    def overall_health(self) -> HealthLevel:
        if not self.reachable:
            return HealthLevel.CRITICAL
        levels = [s.level for s in self.signals]
        if HealthLevel.CRITICAL in levels:
            return HealthLevel.CRITICAL
        if HealthLevel.DEGRADED in levels:
            return HealthLevel.DEGRADED
        if HealthLevel.UNKNOWN in levels:
            return HealthLevel.UNKNOWN
        return HealthLevel.HEALTHY


@dataclass
class FabricHealthReport:
    """Complete fabric health assessment with citations."""
    timestamp: float
    fetch_results: list[DataFetchResult] = field(default_factory=list)
    devices: list[DeviceHealth] = field(default_factory=list)
    signals: list[HealthSignal] = field(default_factory=list)
    alarm_counts: dict[str, int] = field(default_factory=dict)
    partial: bool = False
    incomplete_sources: list[str] = field(default_factory=list)

    @property
    def overall_health(self) -> HealthLevel:
        all_levels = [s.level for s in self.signals]
        for d in self.devices:
            all_levels.append(d.overall_health)
        if HealthLevel.CRITICAL in all_levels:
            return HealthLevel.CRITICAL
        if HealthLevel.DEGRADED in all_levels:
            return HealthLevel.DEGRADED
        if HealthLevel.UNKNOWN in all_levels:
            return HealthLevel.UNKNOWN
        return HealthLevel.HEALTHY


async def _fetch_with_tracking(
    client: VManageClient,
    source: DataSource,
    endpoint: str,
    params: dict | None = None,
    timeout_s: float = 45.0,
) -> DataFetchResult:
    """Fetch an endpoint and track success/failure/duration.

    Timeout covers the entire call including authentication (which can
    take 10-20s on slow sandbox instances).
    """
    start = time.monotonic()
    try:
        data = await asyncio.wait_for(
            client.get(endpoint, params=params),
            timeout=timeout_s,
        )
        duration = (time.monotonic() - start) * 1000
        return DataFetchResult(
            source=source,
            success=True,
            data=data,
            duration_ms=round(duration, 1),
        )
    except asyncio.TimeoutError:
        duration = (time.monotonic() - start) * 1000
        return DataFetchResult(
            source=source,
            success=False,
            error=f"Timed out after {timeout_s:.0f}s",
            duration_ms=round(duration, 1),
        )
    except Exception as e:
        duration = (time.monotonic() - start) * 1000
        return DataFetchResult(
            source=source,
            success=False,
            error=f"{type(e).__name__}: {str(e)}",
            duration_ms=round(duration, 1),
        )


def _parse_bfd_count(value) -> int:
    """Safely parse BFD session count from device data (can be '--' or int)."""
    if value is None or value == "--":
        return 0
    try:
        return int(value)
    except (ValueError, TypeError):
        return 0


def _parse_control_count(value) -> int:
    """Safely parse control connection count from device data."""
    if value is None or value == "--":
        return 0
    try:
        return int(value)
    except (ValueError, TypeError):
        return 0


def compute_device_health(raw_device: dict) -> DeviceHealth:
    """Compute health signals for a single device from the device list entry."""
    hostname = raw_device.get("host-name", "N/A")
    system_ip = raw_device.get("system-ip", "N/A")
    site_id = str(raw_device.get("site-id", "N/A"))
    device_type = raw_device.get("device-type", "N/A")
    device_model = raw_device.get("device-model", "N/A")
    reachable = raw_device.get("reachability", "").lower() == "reachable"
    bfd = _parse_bfd_count(raw_device.get("bfdSessions"))
    ctrl = _parse_control_count(raw_device.get("controlConnections"))
    state = raw_device.get("state", "N/A")

    dh = DeviceHealth(
        hostname=hostname,
        system_ip=system_ip,
        site_id=site_id,
        device_type=device_type,
        device_model=device_model,
        reachable=reachable,
        bfd_sessions=bfd,
        control_connections=ctrl,
        state=state,
    )

    # Signal: unreachable device
    if not reachable:
        dh.signals.append(HealthSignal(
            level=HealthLevel.CRITICAL,
            component=f"device/{hostname}",
            summary=f"{hostname} is unreachable",
            detail=(
                f"{hostname} ({system_ip}) at site {site_id} is unreachable with "
                f"{bfd} BFD sessions and {ctrl} control connections."
            ),
            source=DataSource.DEVICE_LIST,
            evidence={"reachability": "unreachable", "bfd": bfd, "control": ctrl},
        ))

    # Signal: reachable but no BFD (WAN edges should have BFD)
    if reachable and device_type == "vedge" and bfd == 0:
        dh.signals.append(HealthSignal(
            level=HealthLevel.DEGRADED,
            component=f"device/{hostname}/bfd",
            summary=f"{hostname} is reachable but has 0 BFD sessions",
            detail=(
                f"{hostname} ({system_ip}) is reachable but reports 0 BFD sessions. "
                f"This may indicate transport issues or the device just came online."
            ),
            source=DataSource.DEVICE_LIST,
            evidence={"reachability": "reachable", "bfd": 0},
        ))

    # Signal: reachable but no control connections (WAN edges need control plane)
    if reachable and device_type == "vedge" and ctrl == 0:
        dh.signals.append(HealthSignal(
            level=HealthLevel.DEGRADED,
            component=f"device/{hostname}/control",
            summary=f"{hostname} is reachable but has 0 control connections",
            detail=(
                f"{hostname} ({system_ip}) is reachable but has no control connections "
                f"to vSmart/vBond. It cannot receive policies or route updates."
            ),
            source=DataSource.DEVICE_LIST,
            evidence={"reachability": "reachable", "control": 0},
        ))

    # Signal: device state is not green
    if state == "red":
        dh.signals.append(HealthSignal(
            level=HealthLevel.CRITICAL,
            component=f"device/{hostname}/state",
            summary=f"{hostname} is in red state",
            detail=f"{hostname} ({system_ip}) has device state 'red'.",
            source=DataSource.DEVICE_LIST,
            evidence={"state": state},
        ))
    elif state == "yellow":
        dh.signals.append(HealthSignal(
            level=HealthLevel.DEGRADED,
            component=f"device/{hostname}/state",
            summary=f"{hostname} is in yellow state",
            detail=f"{hostname} ({system_ip}) has device state 'yellow', indicating partial issues.",
            source=DataSource.DEVICE_LIST,
            evidence={"state": state},
        ))

    return dh


def compute_alarm_signals(alarm_counts: dict[str, int]) -> list[HealthSignal]:
    """Compute health signals from alarm counts."""
    signals = []

    critical = alarm_counts.get("Critical", 0)
    major = alarm_counts.get("Major", 0)

    if critical > 0:
        signals.append(HealthSignal(
            level=HealthLevel.CRITICAL,
            component="alarms",
            summary=f"{critical} critical alarm(s) active",
            detail=f"There are {critical} critical alarms requiring immediate attention.",
            source=DataSource.ALARM_COUNT,
            evidence=alarm_counts,
        ))
    elif major > 0:
        signals.append(HealthSignal(
            level=HealthLevel.DEGRADED,
            component="alarms",
            summary=f"{major} major alarm(s) active",
            detail=f"There are {major} major alarms that should be investigated.",
            source=DataSource.ALARM_COUNT,
            evidence=alarm_counts,
        ))

    return signals


async def assess_fabric_health(client: VManageClient) -> FabricHealthReport:
    """Collect data from vManage and compute health signals.

    This is the core health assessment. It:
    1. Fetches device list and alarm counts concurrently
    2. Computes health signals for each device
    3. Computes alarm-level signals
    4. Reports partial results if any fetch fails
    """
    report = FabricHealthReport(timestamp=time.time())

    # Fetch concurrently with timeout tracking
    device_fetch, alarm_fetch = await asyncio.gather(
        _fetch_with_tracking(client, DataSource.DEVICE_LIST, "/dataservice/device"),
        _fetch_with_tracking(client, DataSource.ALARM_COUNT, "/dataservice/alarms/count"),
    )

    report.fetch_results = [device_fetch, alarm_fetch]

    # Process device data
    if device_fetch.success and device_fetch.data:
        devices_raw = device_fetch.data.get("data", [])
        for raw in devices_raw:
            dh = compute_device_health(raw)
            report.devices.append(dh)
    else:
        report.partial = True
        report.incomplete_sources.append(device_fetch.source.value)

    # Process alarm data
    if alarm_fetch.success and alarm_fetch.data:
        raw_counts = alarm_fetch.data.get("data", [])
        if raw_counts and "severity" in raw_counts[0]:
            for item in raw_counts:
                report.alarm_counts[item.get("severity", "Unknown")] = item.get("count", 0)
        alarm_signals = compute_alarm_signals(report.alarm_counts)
        report.signals.extend(alarm_signals)
    else:
        report.partial = True
        report.incomplete_sources.append(alarm_fetch.source.value)

    return report


async def assess_device_health(
    client: VManageClient,
    system_ip: str,
) -> tuple[DeviceHealth | None, list[DataFetchResult]]:
    """Deep health assessment for a single device.

    Fetches device list, BFD sessions, control connections, and system status
    concurrently, then computes health signals.
    """
    fetch_results: list[DataFetchResult] = []

    # Fetch all data concurrently
    device_fetch, bfd_fetch, control_fetch, status_fetch = await asyncio.gather(
        _fetch_with_tracking(client, DataSource.DEVICE_LIST, "/dataservice/device"),
        _fetch_with_tracking(
            client, DataSource.BFD_SESSIONS,
            "/dataservice/device/bfd/sessions",
            params={"deviceId": system_ip},
        ),
        _fetch_with_tracking(
            client, DataSource.CONTROL_CONNECTIONS,
            "/dataservice/device/control/connections",
            params={"deviceId": system_ip},
        ),
        _fetch_with_tracking(
            client, DataSource.SYSTEM_STATUS,
            "/dataservice/device/system/status",
            params={"deviceId": system_ip},
        ),
    )

    fetch_results = [device_fetch, bfd_fetch, control_fetch, status_fetch]

    # Find the target device
    if not device_fetch.success or not device_fetch.data:
        return None, fetch_results

    devices_raw = device_fetch.data.get("data", [])
    target = next(
        (d for d in devices_raw
         if d.get("system-ip") == system_ip or d.get("deviceId") == system_ip),
        None,
    )

    if target is None:
        return None, fetch_results

    dh = compute_device_health(target)

    # Enrich with BFD details
    if bfd_fetch.success and bfd_fetch.data:
        sessions = bfd_fetch.data.get("data", [])
        down_sessions = [s for s in sessions if s.get("state", "").lower() != "up"]
        if down_sessions:
            dh.signals.append(HealthSignal(
                level=HealthLevel.DEGRADED,
                component=f"device/{dh.hostname}/bfd-detail",
                summary=f"{len(down_sessions)} BFD session(s) not in up state",
                detail=(
                    f"Out of {len(sessions)} BFD sessions, {len(down_sessions)} are not up. "
                    f"Affected peers: {', '.join(s.get('system-ip', '?') for s in down_sessions[:5])}."
                ),
                source=DataSource.BFD_SESSIONS,
                evidence={
                    "total": len(sessions),
                    "down": len(down_sessions),
                    "down_peers": [s.get("system-ip") for s in down_sessions[:5]],
                },
            ))

    # Enrich with control connection details
    if control_fetch.success and control_fetch.data:
        connections = control_fetch.data.get("data", [])
        non_up = [c for c in connections if c.get("state", "").lower() != "up"]
        if non_up:
            dh.signals.append(HealthSignal(
                level=HealthLevel.DEGRADED,
                component=f"device/{dh.hostname}/control-detail",
                summary=f"{len(non_up)} control connection(s) not up",
                detail=(
                    f"Out of {len(connections)} control connections, {len(non_up)} are not in 'up' state. "
                    f"Affected peers: {', '.join(c.get('system-ip', '?') for c in non_up[:5])}."
                ),
                source=DataSource.CONTROL_CONNECTIONS,
                evidence={
                    "total": len(connections),
                    "not_up": len(non_up),
                },
            ))

    # Enrich with system resource checks
    if status_fetch.success and status_fetch.data:
        statuses = status_fetch.data.get("data", [])
        if statuses:
            s = statuses[0]
            try:
                cpu_5 = float(s.get("min5_avg", 0))
                if cpu_5 > 90:
                    dh.signals.append(HealthSignal(
                        level=HealthLevel.CRITICAL,
                        component=f"device/{dh.hostname}/cpu",
                        summary=f"CPU load is critically high ({cpu_5:.1f}%)",
                        detail=f"5-minute CPU average is {cpu_5:.1f}%, which may impact forwarding.",
                        source=DataSource.SYSTEM_STATUS,
                        evidence={"cpu_5min": cpu_5},
                    ))
                elif cpu_5 > 70:
                    dh.signals.append(HealthSignal(
                        level=HealthLevel.DEGRADED,
                        component=f"device/{dh.hostname}/cpu",
                        summary=f"CPU load is elevated ({cpu_5:.1f}%)",
                        detail=f"5-minute CPU average is {cpu_5:.1f}%.",
                        source=DataSource.SYSTEM_STATUS,
                        evidence={"cpu_5min": cpu_5},
                    ))
            except (ValueError, TypeError):
                pass

            try:
                mem_used = int(s.get("mem_used", 0))
                mem_free = int(s.get("mem_free", 1))
                mem_pct = (mem_used / (mem_used + mem_free)) * 100 if (mem_used + mem_free) > 0 else 0
                if mem_pct > 90:
                    dh.signals.append(HealthSignal(
                        level=HealthLevel.CRITICAL,
                        component=f"device/{dh.hostname}/memory",
                        summary=f"Memory usage is critically high ({mem_pct:.0f}%)",
                        detail=f"Memory usage is {mem_pct:.0f}% ({mem_used} used, {mem_free} free).",
                        source=DataSource.SYSTEM_STATUS,
                        evidence={"mem_used": mem_used, "mem_free": mem_free, "mem_pct": mem_pct},
                    ))
                elif mem_pct > 80:
                    dh.signals.append(HealthSignal(
                        level=HealthLevel.DEGRADED,
                        component=f"device/{dh.hostname}/memory",
                        summary=f"Memory usage is elevated ({mem_pct:.0f}%)",
                        detail=f"Memory usage is {mem_pct:.0f}% ({mem_used} used, {mem_free} free).",
                        source=DataSource.SYSTEM_STATUS,
                        evidence={"mem_used": mem_used, "mem_free": mem_free, "mem_pct": mem_pct},
                    ))
            except (ValueError, TypeError):
                pass

    return dh, fetch_results
