"""Normalized topology and action context derived from read-only vManage evidence."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field

TOPOLOGY_SCHEMA_VERSION = 1
ACTION_SCHEMA_VERSION = 1
DEVICE_SOURCE = "GET /dataservice/device"
BFD_SOURCE = "GET /dataservice/device/bfd/sessions"
CONTROL_SOURCE = "GET /dataservice/device/control/connections"
ALARM_SOURCE = "GET /dataservice/alarms"
EVENT_SOURCE = "GET /dataservice/event"
MAX_ACTION_EVENT_ROWS = 1_000

HealthStatus = Literal["healthy", "degraded", "critical", "unknown"]
SourceStatus = Literal["ok", "partial", "failed"]
NodeKind = Literal["site", "controller", "edge", "tloc"]
EdgeKind = Literal["membership", "ownership", "bfd", "control"]
ActionPriority = Literal["P1", "P2", "P3", "P4"]

_STATUS_WEIGHT: dict[str, int] = {
    "unknown": 0,
    "healthy": 1,
    "degraded": 2,
    "critical": 3,
}
_SEVERITY_WEIGHT = {
    "critical": 4,
    "major": 3,
    "medium": 2,
    "minor": 1,
    "warning": 1,
    "info": 0,
}


class ConnectedContextClient(Protocol):
    """Read-only client operations needed to collect connected context."""

    async def get(self, endpoint: str, params: dict | None = None) -> dict: ...


class EvidenceSource(BaseModel):
    """A source used to derive connected-context objects."""

    model_config = ConfigDict(extra="forbid")

    id: str
    label: str
    state: SourceStatus = "ok"
    detail: str | None = None


class TopologyNode(BaseModel):
    """A normalized site, device, or transport-locator node."""

    model_config = ConfigDict(extra="forbid")

    id: str
    kind: NodeKind
    label: str
    status: HealthStatus
    site_id: str | None = None
    system_ip: str | None = None
    device_type: str | None = None
    device_uuid: str | None = None
    model: str | None = None
    color: str | None = None
    reachable: bool | None = None
    evidence: list[str] = Field(min_length=1)


class TopologyEdge(BaseModel):
    """A normalized membership, ownership, tunnel, or control relationship."""

    model_config = ConfigDict(extra="forbid")

    id: str
    kind: EdgeKind
    source: str
    target: str
    status: HealthStatus
    label: str
    evidence: list[str] = Field(min_length=1)


class TopologySummary(BaseModel):
    """Counts used to render and validate a topology graph."""

    model_config = ConfigDict(extra="forbid")

    sites: int = 0
    controllers: int = 0
    edges: int = 0
    tlocs: int = 0
    tunnels: int = 0
    control_links: int = 0
    unreachable: int = 0


class TopologyGraph(BaseModel):
    """Versioned graph of current SD-WAN topology evidence."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = TOPOLOGY_SCHEMA_VERSION
    generated_at: datetime
    partial: bool = False
    summary: TopologySummary
    nodes: list[TopologyNode]
    edges: list[TopologyEdge]
    sources: list[EvidenceSource]


class ActionEvidence(BaseModel):
    """One normalized alarm or event supporting an action item."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["alarm", "event"]
    label: str
    severity: str
    hostname: str
    system_ip: str
    timestamp: datetime | None = None
    source: str


class ActionItem(BaseModel):
    """A prioritized group of related operational evidence."""

    model_config = ConfigDict(extra="forbid")

    id: str
    priority: ActionPriority
    category: str
    title: str
    site_id: str
    system_ips: list[str]
    alarm_count: int
    event_count: int
    latest_at: datetime | None
    evidence: list[ActionEvidence]
    recommended_checks: list[str]


class ActionInbox(BaseModel):
    """Versioned, prioritized actions derived from alarms and events."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = ACTION_SCHEMA_VERSION
    generated_at: datetime
    partial: bool = False
    summary: dict[ActionPriority, int]
    items: list[ActionItem]
    sources: list[EvidenceSource]


def _text(value: Any, default: str = "unknown") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _device_status(device: Mapping[str, Any]) -> HealthStatus:
    reachability = _text(device.get("reachability"), "").lower()
    state = _text(device.get("state"), "").lower()
    if reachability == "unreachable" or state in {"red", "down", "failed"}:
        return "critical"
    if state in {"yellow", "degraded", "warning"}:
        return "degraded"
    if reachability == "reachable" or state in {"green", "up", "normal"}:
        return "healthy"
    return "unknown"


def _link_status(value: Any) -> HealthStatus:
    state = _text(value, "").lower()
    if state in {"up", "green", "active", "connected"}:
        return "healthy"
    if state in {"down", "red", "inactive", "failed", "disconnected"}:
        return "critical"
    return "unknown"


def _worse(first: HealthStatus, second: HealthStatus) -> HealthStatus:
    return first if _STATUS_WEIGHT[first] >= _STATUS_WEIGHT[second] else second


def _endpoint_source(endpoint: str, system_ip: str) -> str:
    return f"{endpoint}?deviceId={system_ip}"


def _tloc_id(system_ip: str, color: str, address: str) -> str:
    return f"tloc:{system_ip}:{color}:{address}"


def _upsert_node(nodes: dict[str, TopologyNode], node: TopologyNode) -> None:
    existing = nodes.get(node.id)
    if existing is None:
        nodes[node.id] = node
        return
    existing.status = _worse(existing.status, node.status)
    existing.evidence = sorted(set(existing.evidence + node.evidence))


def _upsert_edge(edges: dict[str, TopologyEdge], edge: TopologyEdge) -> None:
    existing = edges.get(edge.id)
    if existing is None:
        edges[edge.id] = edge
        return
    existing.status = _worse(existing.status, edge.status)
    existing.evidence = sorted(set(existing.evidence + edge.evidence))


def _add_device_nodes(
    devices: Sequence[Mapping[str, Any]],
    nodes: dict[str, TopologyNode],
    relationships: dict[str, TopologyEdge],
) -> dict[str, Mapping[str, Any]]:
    devices_by_ip: dict[str, Mapping[str, Any]] = {}
    for device in devices:
        system_ip = _text(device.get("system-ip", device.get("system_ip")))
        if system_ip == "unknown":
            continue
        devices_by_ip[system_ip] = device
        device_type = _text(device.get("device-type", device.get("device_type")))
        kind: NodeKind = "edge" if device_type == "vedge" else "controller"
        status = _device_status(device)
        site_id = _text(device.get("site-id", device.get("site_id")))
        device_id = f"device:{system_ip}"
        _upsert_node(
            nodes,
            TopologyNode(
                id=device_id,
                kind=kind,
                label=_text(device.get("host-name", device.get("hostname")), system_ip),
                status=status,
                site_id=site_id,
                system_ip=system_ip,
                device_type=device_type,
                device_uuid=(
                    _text(device.get("uuid"), "")
                    or _text(device.get("device-uuid", device.get("device_uuid")), "")
                    or None
                ),
                model=_text(device.get("device-model", device.get("model"))),
                reachable=(
                    True
                    if _text(device.get("reachability"), "").lower() == "reachable"
                    else False
                    if _text(device.get("reachability"), "").lower() == "unreachable"
                    else None
                ),
                evidence=[DEVICE_SOURCE],
            ),
        )
        if kind == "edge":
            site_node_id = f"site:{site_id}"
            _upsert_node(
                nodes,
                TopologyNode(
                    id=site_node_id,
                    kind="site",
                    label=f"Site {site_id}",
                    status=status,
                    site_id=site_id,
                    evidence=[DEVICE_SOURCE],
                ),
            )
            _upsert_edge(
                relationships,
                TopologyEdge(
                    id=f"membership:{site_node_id}:{device_id}",
                    kind="membership",
                    source=site_node_id,
                    target=device_id,
                    status=status,
                    label="site membership",
                    evidence=[DEVICE_SOURCE],
                ),
            )
    return devices_by_ip


def _add_bfd_topology(
    bfd_sessions: Mapping[str, Sequence[Mapping[str, Any]]],
    devices_by_ip: Mapping[str, Mapping[str, Any]],
    nodes: dict[str, TopologyNode],
    relationships: dict[str, TopologyEdge],
) -> set[str]:
    source_labels: set[str] = set()
    for local_system_ip, sessions in bfd_sessions.items():
        source = _endpoint_source(BFD_SOURCE, local_system_ip)
        source_labels.add(source)
        for session in sessions:
            peer_system_ip = _text(session.get("system-ip", session.get("system_ip")))
            local_color = _text(session.get("local-color", session.get("local_color")))
            remote_color = _text(session.get("color", session.get("remote-color")))
            source_ip = _text(session.get("src-ip", session.get("source-ip")))
            destination_ip = _text(session.get("dst-ip", session.get("dest-ip")))
            status = _link_status(session.get("state"))
            local_tloc_id = _tloc_id(local_system_ip, local_color, source_ip)
            remote_tloc_id = _tloc_id(peer_system_ip, remote_color, destination_ip)

            for tloc_id, system_ip, color in (
                (local_tloc_id, local_system_ip, local_color),
                (remote_tloc_id, peer_system_ip, remote_color),
            ):
                device = devices_by_ip.get(system_ip, {})
                _upsert_node(
                    nodes,
                    TopologyNode(
                        id=tloc_id,
                        kind="tloc",
                        label=f"{_text(device.get('host-name'), system_ip)} / {color}",
                        status=status,
                        site_id=_text(device.get("site-id")) if device else None,
                        system_ip=system_ip,
                        color=color,
                        evidence=[source],
                    ),
                )
                device_id = f"device:{system_ip}"
                if device_id in nodes:
                    _upsert_edge(
                        relationships,
                        TopologyEdge(
                            id=f"ownership:{device_id}:{tloc_id}",
                            kind="ownership",
                            source=device_id,
                            target=tloc_id,
                            status=status,
                            label=f"{color} transport",
                            evidence=[source],
                        ),
                    )

            first_tloc, second_tloc = sorted((local_tloc_id, remote_tloc_id))
            _upsert_edge(
                relationships,
                TopologyEdge(
                    id=f"bfd:{first_tloc}:{second_tloc}",
                    kind="bfd",
                    source=first_tloc,
                    target=second_tloc,
                    status=status,
                    label=f"{local_color} to {remote_color}",
                    evidence=[source],
                ),
            )
    return source_labels


def _add_control_topology(
    control_connections: Mapping[str, Sequence[Mapping[str, Any]]],
    nodes: dict[str, TopologyNode],
    relationships: dict[str, TopologyEdge],
) -> set[str]:
    source_labels: set[str] = set()
    for local_system_ip, connections in control_connections.items():
        source = _endpoint_source(CONTROL_SOURCE, local_system_ip)
        source_labels.add(source)
        local_device_id = f"device:{local_system_ip}"
        for connection in connections:
            peer_system_ip = _text(connection.get("system-ip", connection.get("system_ip")))
            peer_device_id = f"device:{peer_system_ip}"
            status = _link_status(connection.get("state"))
            if peer_device_id not in nodes:
                _upsert_node(
                    nodes,
                    TopologyNode(
                        id=peer_device_id,
                        kind="controller",
                        label=peer_system_ip,
                        status=status,
                        system_ip=peer_system_ip,
                        device_type=_text(
                            connection.get("peer-type", connection.get("peer_type"))
                        ),
                        evidence=[source],
                    ),
                )
            if local_device_id in nodes:
                _upsert_edge(
                    relationships,
                    TopologyEdge(
                        id=f"control:{local_device_id}:{peer_device_id}",
                        kind="control",
                        source=local_device_id,
                        target=peer_device_id,
                        status=status,
                        label=_text(
                            connection.get("peer-type", connection.get("peer_type")),
                            "control",
                        ),
                        evidence=[source],
                    ),
                )
    return source_labels


def build_topology(
    devices: Sequence[Mapping[str, Any]],
    bfd_sessions: Mapping[str, Sequence[Mapping[str, Any]]],
    control_connections: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    generated_at: datetime | None = None,
    partial: bool = False,
    source_states: Mapping[str, SourceStatus] | None = None,
) -> TopologyGraph:
    """Build a deterministic graph from normalized read-only API responses."""
    nodes: dict[str, TopologyNode] = {}
    relationships: dict[str, TopologyEdge] = {}
    devices_by_ip = _add_device_nodes(devices, nodes, relationships)
    source_labels = {DEVICE_SOURCE}
    source_labels.update(
        _add_bfd_topology(bfd_sessions, devices_by_ip, nodes, relationships)
    )
    source_labels.update(
        _add_control_topology(control_connections, nodes, relationships)
    )

    ordered_nodes = sorted(nodes.values(), key=lambda node: node.id)
    ordered_edges = sorted(relationships.values(), key=lambda edge: edge.id)
    states = source_states or {}
    sources = [
        EvidenceSource(
            id=label,
            label=label,
            state=states.get(label, "ok"),
        )
        for label in sorted(source_labels)
    ]
    summary = TopologySummary(
        sites=sum(node.kind == "site" for node in ordered_nodes),
        controllers=sum(node.kind == "controller" for node in ordered_nodes),
        edges=sum(node.kind == "edge" for node in ordered_nodes),
        tlocs=sum(node.kind == "tloc" for node in ordered_nodes),
        tunnels=sum(edge.kind == "bfd" for edge in ordered_edges),
        control_links=sum(edge.kind == "control" for edge in ordered_edges),
        unreachable=sum(
            node.kind in {"edge", "controller"} and node.status == "critical"
            for node in ordered_nodes
        ),
    )
    return TopologyGraph(
        generated_at=generated_at or datetime.now(UTC),
        partial=partial,
        summary=summary,
        nodes=ordered_nodes,
        edges=ordered_edges,
        sources=sources,
    )


def _event_timestamp(value: Any) -> datetime | None:
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return None
    if timestamp > 10_000_000_000:
        timestamp /= 1_000
    try:
        return datetime.fromtimestamp(timestamp, tz=UTC)
    except (OSError, OverflowError, ValueError):
        return None


def _action_category(label: str) -> str:
    normalized = label.lower()
    if "control" in normalized or "omp" in normalized:
        return "control-plane"
    if "bfd" in normalized or "tunnel" in normalized:
        return "overlay-path"
    if "unreach" in normalized or "reachability" in normalized:
        return "reachability"
    if any(term in normalized for term in ("cpu", "memory", "disk", "resource")):
        return "resources"
    if any(term in normalized for term in ("reboot", "config", "policy", "template")):
        return "change"
    return "other"


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "unknown"


def _priority(severity_weight: int) -> ActionPriority:
    if severity_weight >= 4:
        return "P1"
    if severity_weight == 3:
        return "P2"
    if severity_weight == 2:
        return "P3"
    return "P4"


def _recommended_checks(category: str) -> list[str]:
    checks = {
        "control-plane": [
            "Verify control connections and controller reachability.",
            "Correlate recent policy or certificate events.",
        ],
        "overlay-path": [
            "Inspect BFD peers, colors, loss, latency, and jitter.",
            "Compare both ends of the affected tunnel.",
        ],
        "reachability": [
            "Confirm management reachability and last contact time.",
            "Check whether impact is isolated to one site.",
        ],
        "resources": [
            "Inspect CPU, memory, disk, and process health.",
            "Correlate resource pressure with recent events.",
        ],
        "change": [
            "Review the most recent approved configuration change.",
            "Compare pre-change and current fabric evidence.",
        ],
        "other": [
            "Inspect the cited alarm and event evidence.",
            "Confirm scope before escalating or changing the network.",
        ],
    }
    return checks[category]


def build_action_inbox(
    devices: Sequence[Mapping[str, Any]],
    alarms: Sequence[Mapping[str, Any]],
    events: Sequence[Mapping[str, Any]],
    *,
    generated_at: datetime | None = None,
    partial: bool = False,
    source_states: Mapping[str, SourceStatus] | None = None,
) -> ActionInbox:
    """Group alarms and events into deterministic, prioritized operator actions."""
    site_by_system_ip = {
        _text(device.get("system-ip", device.get("system_ip"))): _text(
            device.get("site-id", device.get("site_id"))
        )
        for device in devices
    }
    groups: dict[tuple[str, str], dict[str, Any]] = {}

    for kind, rows, source in (
        ("alarm", alarms, ALARM_SOURCE),
        ("event", events, EVENT_SOURCE),
    ):
        evidence_kind = cast(Literal["alarm", "event"], kind)
        for row in rows:
            label = _text(
                row.get("type", row.get("eventname", row.get("event_name"))),
                "Operational signal",
            )
            category = _action_category(label)
            system_ip = _text(
                row.get("system-ip", row.get("system_ip", row.get("deviceId")))
            )
            site_id = site_by_system_ip.get(system_ip, _text(row.get("site-id")))
            severity = _text(row.get("severity", row.get("severity_level")), "Unknown")
            timestamp = _event_timestamp(row.get("entry_time", row.get("receive_time")))
            group = groups.setdefault(
                (site_id, category),
                {
                    "severity_weight": 0,
                    "system_ips": set(),
                    "alarm_count": 0,
                    "event_count": 0,
                    "latest_at": None,
                    "evidence": {},
                },
            )
            group["severity_weight"] = max(
                group["severity_weight"],
                _SEVERITY_WEIGHT.get(severity.lower(), 0),
            )
            if system_ip != "unknown":
                group["system_ips"].add(system_ip)
            group[f"{kind}_count"] += 1
            if timestamp is not None and (
                group["latest_at"] is None or timestamp > group["latest_at"]
            ):
                group["latest_at"] = timestamp
            hostname = _text(
                row.get("host-name", row.get("host_name")),
                system_ip,
            )
            evidence_record = ActionEvidence(
                kind=evidence_kind,
                label=label,
                severity=severity,
                hostname=hostname,
                system_ip=system_ip,
                timestamp=timestamp,
                source=source,
            )
            evidence_key = (evidence_kind, label, severity, hostname, system_ip)
            existing_evidence = group["evidence"].get(evidence_key)
            if (
                existing_evidence is None
                or existing_evidence.timestamp is None
                or (timestamp is not None and timestamp > existing_evidence.timestamp)
            ):
                group["evidence"][evidence_key] = evidence_record

    def evidence_order(record: ActionEvidence) -> tuple[int, float, str]:
        return (
            -_SEVERITY_WEIGHT.get(record.severity.lower(), 0),
            -(record.timestamp.timestamp() if record.timestamp else 0),
            record.label,
        )

    items = []
    for (site_id, category), group in groups.items():
        priority = _priority(group["severity_weight"])
        items.append(
            ActionItem(
                id=f"action:{_slug(site_id)}:{_slug(category)}",
                priority=priority,
                category=category,
                title=f"Investigate {category.replace('-', ' ')} signals at site {site_id}",
                site_id=site_id,
                system_ips=sorted(group["system_ips"]),
                alarm_count=group["alarm_count"],
                event_count=group["event_count"],
                latest_at=group["latest_at"],
                evidence=sorted(group["evidence"].values(), key=evidence_order)[:25],
                recommended_checks=_recommended_checks(category),
            )
        )
    items.sort(
        key=lambda item: (
            int(item.priority[1]),
            -(item.latest_at.timestamp() if item.latest_at else 0),
            item.id,
        )
    )
    summary: dict[ActionPriority, int] = {"P1": 0, "P2": 0, "P3": 0, "P4": 0}
    for item in items:
        summary[item.priority] += 1
    states = source_states or {}
    sources = [
        EvidenceSource(id=source, label=source, state=states.get(source, "ok"))
        for source in (DEVICE_SOURCE, ALARM_SOURCE, EVENT_SOURCE)
    ]
    return ActionInbox(
        generated_at=generated_at or datetime.now(UTC),
        partial=partial,
        summary=summary,
        items=items,
        sources=sources,
    )


def _response_rows(payload: Any) -> list[Mapping[str, Any]]:
    if not isinstance(payload, Mapping):
        return []
    rows = payload.get("data", [])
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, Mapping)]


async def _fetch_rows(
    client: ConnectedContextClient,
    endpoint: str,
    params: dict | None = None,
) -> tuple[list[Mapping[str, Any]], bool]:
    try:
        return _response_rows(await client.get(endpoint, params=params)), True
    except Exception:
        return [], False


async def collect_topology(
    client: ConnectedContextClient,
    *,
    detail_limit: int = 100,
    concurrency: int = 8,
) -> TopologyGraph:
    """Collect a bounded current topology while preserving partial evidence."""
    if not 1 <= detail_limit <= 500:
        raise ValueError("detail_limit must be between 1 and 500")
    if not 1 <= concurrency <= 32:
        raise ValueError("concurrency must be between 1 and 32")

    devices, devices_ok = await _fetch_rows(client, "/dataservice/device")
    source_states: dict[str, SourceStatus] = {
        DEVICE_SOURCE: "ok" if devices_ok else "failed"
    }
    edge_system_ips = [
        _text(device.get("system-ip", device.get("system_ip")))
        for device in devices
        if _text(device.get("device-type", device.get("device_type"))) == "vedge"
    ]
    selected_system_ips = [
        system_ip for system_ip in edge_system_ips if system_ip != "unknown"
    ][:detail_limit]
    semaphore = asyncio.Semaphore(concurrency)

    async def bounded_fetch(
        endpoint: str,
        system_ip: str,
    ) -> tuple[str, str, list[Mapping[str, Any]], bool]:
        async with semaphore:
            rows, success = await _fetch_rows(
                client,
                endpoint,
                params={"deviceId": system_ip},
            )
        return endpoint, system_ip, rows, success

    requests = [
        bounded_fetch(endpoint, system_ip)
        for system_ip in selected_system_ips
        for endpoint in (
            "/dataservice/device/bfd/sessions",
            "/dataservice/device/control/connections",
        )
    ]
    results = await asyncio.gather(*requests)
    bfd_sessions: dict[str, Sequence[Mapping[str, Any]]] = {}
    control_connections: dict[str, Sequence[Mapping[str, Any]]] = {}
    detail_failed = False
    for endpoint, system_ip, rows, success in results:
        source = _endpoint_source(
            BFD_SOURCE if endpoint.endswith("bfd/sessions") else CONTROL_SOURCE,
            system_ip,
        )
        source_states[source] = "ok" if success else "failed"
        detail_failed = detail_failed or not success
        if endpoint.endswith("bfd/sessions"):
            bfd_sessions[system_ip] = rows
        else:
            control_connections[system_ip] = rows

    return build_topology(
        devices,
        bfd_sessions,
        control_connections,
        partial=(not devices_ok or detail_failed or len(edge_system_ips) > detail_limit),
        source_states=source_states,
    )


def _event_query(hours_back: int) -> str:
    return json.dumps(
        {
            "query": {
                "condition": "AND",
                "rules": [
                    {
                        "value": [str(hours_back)],
                        "field": "entry_time",
                        "type": "date",
                        "operator": "last_n_hours",
                    }
                ],
            },
            "sort": [{"field": "entry_time", "type": "date", "order": "desc"}],
        },
        separators=(",", ":"),
    )


def _recent_events(
    rows: Sequence[Mapping[str, Any]],
    hours_back: int,
) -> list[Mapping[str, Any]]:
    cutoff = datetime.now(UTC).timestamp() - (hours_back * 60 * 60)
    recent = []
    for row in rows:
        timestamp = _event_timestamp(row.get("entry_time", row.get("receive_time")))
        if timestamp is not None and timestamp.timestamp() >= cutoff:
            recent.append((timestamp, row))
    recent.sort(key=lambda item: item[0], reverse=True)
    return [row for _timestamp, row in recent[:MAX_ACTION_EVENT_ROWS]]


async def collect_action_inbox(
    client: ConnectedContextClient,
    *,
    event_hours: int = 24,
) -> ActionInbox:
    """Collect device, alarm, and event evidence for the operator actions inbox."""
    if not 1 <= event_hours <= 168:
        raise ValueError("event_hours must be between 1 and 168")
    device_result, alarm_result, event_result = await asyncio.gather(
        _fetch_rows(client, "/dataservice/device"),
        _fetch_rows(client, "/dataservice/alarms"),
        _fetch_rows(
            client,
            "/dataservice/event",
            params={"query": _event_query(event_hours)},
        ),
    )
    devices, devices_ok = device_result
    alarms, alarms_ok = alarm_result
    events, events_ok = event_result
    events = _recent_events(events, event_hours)
    source_states: dict[str, SourceStatus] = {
        DEVICE_SOURCE: "ok" if devices_ok else "failed",
        ALARM_SOURCE: "ok" if alarms_ok else "failed",
        EVENT_SOURCE: "ok" if events_ok else "failed",
    }
    return build_action_inbox(
        devices,
        alarms,
        events,
        partial=not all((devices_ok, alarms_ok, events_ok)),
        source_states=source_states,
    )
