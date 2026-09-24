"""Versioned fabric snapshots, deterministic comparisons, and SLA trends."""

from __future__ import annotations

import asyncio
import hashlib
import math
import sqlite3
import threading
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import fmean
from typing import Any, Literal, Protocol, cast
from uuid import uuid4

from cryptography.fernet import InvalidToken
from pydantic import BaseModel, ConfigDict, Field

from cisco_vmanage_mcp.services.connected_context import TopologyGraph
from cisco_vmanage_mcp.services.persistence import (
    canonical_json_bytes,
    load_or_create_fernet,
    prepare_private_directory,
    private_sqlite_connection,
)

SNAPSHOT_SCHEMA_VERSION = 1
DEFAULT_RETENTION_DAYS = 30
DEFAULT_MAX_SNAPSHOTS = 720
APP_ROUTE_SOURCE = "GET /dataservice/device/app-route/statistics"

HealthStatus = Literal["healthy", "degraded", "critical", "unknown"]
DeviceKind = Literal["edge", "controller"]
FaultDomain = Literal["branch", "wan", "isp", "internet", "cloud", "saas", "application"]
ComparisonVerdict = Literal["improved", "regressed", "mixed", "unchanged"]

_HEALTH_WEIGHT: dict[str, int] = {
    "healthy": 0,
    "unknown": 1,
    "degraded": 2,
    "critical": 3,
}
_DOMAIN_ORDER: tuple[FaultDomain, ...] = (
    "branch",
    "wan",
    "isp",
    "internet",
    "cloud",
    "saas",
    "application",
)


class SnapshotClient(Protocol):
    """Read-only client operation needed for assurance collection."""

    async def get(self, endpoint: str, params: dict | None = None) -> dict: ...


class ConfigurationSnapshotClient(Protocol):
    """Read-only raw response operation used for opt-in configuration hashing."""

    async def get_raw(self, endpoint: str, params: dict | None = None) -> str: ...


class DeviceSnapshot(BaseModel):
    """Minimal device state retained in a fabric snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    system_ip: str
    hostname: str
    site_id: str
    kind: DeviceKind
    reachable: bool | None
    health: HealthStatus


class SlaSample(BaseModel):
    """A normalized point-in-time transport measurement."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    system_ip: str
    peer_system_ip: str
    site_id: str
    fault_domain: FaultDomain
    local_color: str
    remote_color: str
    state: str
    latency_ms: float | None = None
    jitter_ms: float | None = None
    loss_percent: float | None = None
    availability_percent: float | None = Field(default=None, ge=0, le=100)
    source: str


class FabricSnapshot(BaseModel):
    """Immutable, versioned evidence retained for historical comparison."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = SNAPSHOT_SCHEMA_VERSION
    id: str = Field(default_factory=lambda: str(uuid4()))
    captured_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    health: HealthStatus
    partial: bool
    impact_scope: str = "unknown"
    devices: tuple[DeviceSnapshot, ...] = ()
    alarm_counts: dict[str, int] = Field(default_factory=dict)
    bfd_up: int = 0
    bfd_down: int = 0
    control_up: int = 0
    control_down: int = 0
    configuration_hashes: dict[str, str] = Field(default_factory=dict)
    sla_samples: tuple[SlaSample, ...] = ()
    source_freshness: dict[str, datetime] = Field(default_factory=dict)
    incomplete_sources: tuple[str, ...] = ()


class DeviceChange(BaseModel):
    """Before/after state for one changed device."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    system_ip: str
    hostname: str
    change: Literal["added", "removed", "changed"]
    reachable: dict[Literal["before", "after"], bool | None]
    health: dict[Literal["before", "after"], HealthStatus | None]


class SnapshotComparison(BaseModel):
    """Deterministic delta between two immutable snapshots."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    before_id: str
    after_id: str
    before_at: datetime
    after_at: datetime
    verdict: ComparisonVerdict
    health: dict[Literal["before", "after"], HealthStatus]
    device_changes: tuple[DeviceChange, ...]
    alarm_delta: dict[str, int]
    bfd_up_delta: int
    bfd_down_delta: int
    control_up_delta: int
    control_down_delta: int
    configuration_changes: list[str]


class SlaTrendPoint(BaseModel):
    """Aggregated assurance values for one domain at one snapshot time."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    captured_at: datetime
    latency_ms: float | None = None
    jitter_ms: float | None = None
    loss_percent: float | None = None
    availability_percent: float | None = None


class SlaTrendSeries(BaseModel):
    """Chronological assurance values for one fault domain."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    fault_domain: FaultDomain
    points: tuple[SlaTrendPoint, ...]


class SlaTrends(BaseModel):
    """Versioned trend response across retained snapshots."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = SNAPSHOT_SCHEMA_VERSION
    days: int
    snapshot_count: int
    generated_at: datetime | None
    series: tuple[SlaTrendSeries, ...]


class SourceFreshness(BaseModel):
    """Age and state for one source captured in a snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: str
    observed_at: datetime
    age_seconds: float
    state: Literal["current", "delayed"]


class SnapshotFreshnessReport(BaseModel):
    """Freshness classification for every source in one snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot_id: str
    stale_after_seconds: int
    delayed_count: int
    sources: tuple[SourceFreshness, ...]


class SnapshotStoreError(RuntimeError):
    """Raised when snapshot persistence cannot be initialized safely."""


class SnapshotStore:
    """Persist immutable encrypted snapshots with age and count retention."""

    def __init__(
        self,
        state_dir: Path,
        *,
        retention_days: int = DEFAULT_RETENTION_DAYS,
        max_snapshots: int = DEFAULT_MAX_SNAPSHOTS,
    ) -> None:
        if not 1 <= retention_days <= 3_650:
            raise ValueError("retention_days must be between 1 and 3650")
        if not 1 <= max_snapshots <= 10_000:
            raise ValueError("max_snapshots must be between 1 and 10000")
        self.state_dir = Path(state_dir).expanduser()
        self.database_path = self.state_dir / "snapshots.sqlite3"
        self.key_path = self.state_dir / "snapshots.key"
        self.retention_days = retention_days
        self.max_snapshots = max_snapshots
        self._lock = threading.RLock()

        prepare_private_directory(self.state_dir)
        self._cipher = load_or_create_fernet(
            self.key_path,
            error_type=SnapshotStoreError,
            error_message="Snapshot encryption key is invalid",
        )
        self._migrate()

    def add(self, snapshot: FabricSnapshot) -> FabricSnapshot:
        """Insert one immutable snapshot and apply retention."""
        payload = self._cipher.encrypt(
            canonical_json_bytes(snapshot.model_dump(mode="json"))
        )
        try:
            with self._lock, private_sqlite_connection(self.database_path) as connection:
                connection.execute(
                    "INSERT INTO snapshots (id, captured_at, payload) VALUES (?, ?, ?)",
                    (snapshot.id, snapshot.captured_at.isoformat(), payload),
                )
                self._prune(connection)
        except sqlite3.IntegrityError as exc:
            raise SnapshotStoreError(f"Snapshot already exists: {snapshot.id}") from exc
        return snapshot

    def get(self, snapshot_id: str) -> FabricSnapshot | None:
        """Load one snapshot, quarantining an unreadable record."""
        with self._lock, private_sqlite_connection(self.database_path) as connection:
            row = connection.execute(
                "SELECT captured_at, payload FROM snapshots WHERE id = ?",
                (snapshot_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            return self._decrypt(row[1])
        except SnapshotStoreError:
            self._quarantine(snapshot_id, row[0], row[1])
            return None

    def list(self, *, limit: int | None = None) -> list[FabricSnapshot]:
        """Return newest snapshots while quarantining unreadable records."""
        bounded_limit = self.max_snapshots if limit is None else limit
        if not 1 <= bounded_limit <= self.max_snapshots:
            raise ValueError(f"limit must be between 1 and {self.max_snapshots}")
        with self._lock, private_sqlite_connection(self.database_path) as connection:
            rows = connection.execute(
                "SELECT id, captured_at, payload FROM snapshots "
                "ORDER BY captured_at DESC, rowid DESC LIMIT ?",
                (bounded_limit,),
            ).fetchall()
        snapshots = []
        for snapshot_id, captured_at, payload in rows:
            try:
                snapshots.append(self._decrypt(payload))
            except SnapshotStoreError:
                self._quarantine(snapshot_id, captured_at, payload)
        return snapshots

    def delete(self, snapshot_id: str) -> bool:
        """Delete one retained snapshot."""
        with self._lock, private_sqlite_connection(self.database_path) as connection:
            cursor = connection.execute("DELETE FROM snapshots WHERE id = ?", (snapshot_id,))
        return cursor.rowcount > 0

    def quarantined_count(self) -> int:
        """Return the number of unreadable records isolated from normal reads."""
        with self._lock, private_sqlite_connection(self.database_path) as connection:
            return int(connection.execute("SELECT COUNT(*) FROM snapshot_quarantine").fetchone()[0])

    def _migrate(self) -> None:
        with self._lock, private_sqlite_connection(self.database_path) as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version > SNAPSHOT_SCHEMA_VERSION:
                raise SnapshotStoreError(
                    f"Snapshot schema {version} is newer than supported schema "
                    f"{SNAPSHOT_SCHEMA_VERSION}"
                )
            if version == 0:
                connection.execute(
                    """
                    CREATE TABLE snapshots (
                        id TEXT PRIMARY KEY,
                        captured_at TEXT NOT NULL,
                        payload BLOB NOT NULL
                    )
                    """
                )
                connection.execute(
                    "CREATE INDEX snapshots_captured_at ON snapshots(captured_at DESC)"
                )
                connection.execute(
                    """
                    CREATE TABLE snapshot_quarantine (
                        id TEXT PRIMARY KEY,
                        captured_at TEXT NOT NULL,
                        payload BLOB NOT NULL,
                        quarantined_at TEXT NOT NULL,
                        reason TEXT NOT NULL
                    )
                    """
                )
                connection.execute(f"PRAGMA user_version = {SNAPSHOT_SCHEMA_VERSION}")

    def _prune(self, connection: sqlite3.Connection) -> None:
        cutoff = datetime.now(UTC) - timedelta(days=self.retention_days)
        connection.execute("DELETE FROM snapshots WHERE captured_at < ?", (cutoff.isoformat(),))
        connection.execute(
            """
            DELETE FROM snapshots
            WHERE id IN (
                SELECT id FROM snapshots
                ORDER BY captured_at DESC, rowid DESC
                LIMIT -1 OFFSET ?
            )
            """,
            (self.max_snapshots,),
        )

    def _decrypt(self, payload: bytes) -> FabricSnapshot:
        try:
            return FabricSnapshot.model_validate_json(self._cipher.decrypt(payload))
        except (InvalidToken, ValueError) as exc:
            raise SnapshotStoreError("Snapshot payload is unreadable") from exc

    def _quarantine(self, snapshot_id: str, captured_at: str, payload: bytes) -> None:
        with self._lock, private_sqlite_connection(self.database_path) as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO snapshot_quarantine
                    (id, captured_at, payload, quarantined_at, reason)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    captured_at,
                    payload,
                    datetime.now(UTC).isoformat(),
                    "unreadable-payload",
                ),
            )
            connection.execute("DELETE FROM snapshots WHERE id = ?", (snapshot_id,))


def classify_fault_domain(local_color: str, remote_color: str) -> FaultDomain:
    """Classify a path into an assurance fault domain from transport labels."""
    combined = f"{local_color} {remote_color}".lower()
    if any(term in combined for term in ("application", "app-")):
        return "application"
    if "saas" in combined:
        return "saas"
    if any(term in combined for term in ("cloud", "aws", "azure", "gcp")):
        return "cloud"
    if any(term in combined for term in ("lte", "5g", "3g", "cellular")):
        return "isp"
    if any(term in combined for term in ("internet", "broadband")):
        return "internet"
    if "branch" in combined:
        return "branch"
    return "wan"


def configuration_digest(configuration: str) -> str:
    """Return a stable digest without retaining the raw configuration."""
    return hashlib.sha256(configuration.encode("utf-8")).hexdigest()


def _number(value: Any, *, minimum: float = 0, maximum: float | None = None) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < minimum:
        return None
    if maximum is not None and number > maximum:
        return None
    return number


def _first_value(row: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in row and row[name] not in (None, "", "--"):
            return row[name]
    return None


async def collect_sla_samples(
    client: SnapshotClient,
    topology: TopologyGraph,
    *,
    detail_limit: int = 100,
    concurrency: int = 8,
) -> tuple[tuple[SlaSample, ...], bool, tuple[str, ...]]:
    """Collect bounded tunnel measurements and immediately normalize them."""
    if not 1 <= detail_limit <= 500:
        raise ValueError("detail_limit must be between 1 and 500")
    if not 1 <= concurrency <= 32:
        raise ValueError("concurrency must be between 1 and 32")
    edge_nodes = [
        node for node in topology.nodes if node.kind == "edge" and node.system_ip is not None
    ]
    selected_nodes = edge_nodes[:detail_limit]
    semaphore = asyncio.Semaphore(concurrency)

    async def fetch_tunnels(
        system_ip: str,
    ) -> tuple[str, list[Mapping[str, Any]], bool]:
        try:
            async with semaphore:
                payload = await client.get(
                    "/dataservice/device/app-route/statistics",
                    params={"deviceId": system_ip},
                )
        except Exception:
            return system_ip, [], False
        if not isinstance(payload, Mapping) or not isinstance(payload.get("data"), list):
            return system_ip, [], False
        rows = [row for row in payload["data"] if isinstance(row, Mapping)]
        return system_ip, rows, True

    results = await asyncio.gather(
        *(fetch_tunnels(node.system_ip) for node in selected_nodes if node.system_ip)
    )
    site_by_system_ip = {
        node.system_ip: node.site_id or "unknown"
        for node in edge_nodes
        if node.system_ip is not None
    }
    samples = []
    sources = []
    failed = False
    for system_ip, rows, success in results:
        source = f"{APP_ROUTE_SOURCE}?deviceId={system_ip}"
        sources.append(source)
        failed = failed or not success
        for row in rows:
            local_color = str(
                _first_value(row, "local-color", "local_color") or "unknown"
            )
            remote_color = str(
                _first_value(row, "remote-color", "remote_color", "color") or "unknown"
            )
            state = str(_first_value(row, "state", "status") or "unknown").lower()
            loss_percent = _number(
                _first_value(row, "loss", "mean-loss", "loss-percent", "loss_percentage"),
                maximum=100,
            )
            availability = (
                100.0
                if state in {"up", "active"}
                else 0.0
                if state == "down"
                else round(100 - loss_percent, 3)
                if loss_percent is not None
                else None
            )
            samples.append(
                SlaSample(
                    system_ip=system_ip,
                    peer_system_ip=str(
                        _first_value(row, "system-ip", "system_ip", "peer-system-ip")
                        or "unknown"
                    ),
                    site_id=site_by_system_ip.get(system_ip, "unknown"),
                    fault_domain=classify_fault_domain(local_color, remote_color),
                    local_color=local_color,
                    remote_color=remote_color,
                    state=state,
                    latency_ms=_number(
                        _first_value(
                            row,
                            "average-latency",
                            "mean-latency",
                            "latency",
                            "latency-ms",
                            "latency_ms",
                        )
                    ),
                    jitter_ms=_number(
                        _first_value(
                            row,
                            "average-jitter",
                            "mean-jitter",
                            "jitter",
                            "jitter-ms",
                            "jitter_ms",
                        )
                    ),
                    loss_percent=loss_percent,
                    availability_percent=availability,
                    source=source,
                )
            )
    return (
        tuple(samples),
        failed or len(edge_nodes) > detail_limit,
        tuple(sources),
    )


async def collect_configuration_hashes(
    client: ConfigurationSnapshotClient,
    topology: TopologyGraph,
    *,
    detail_limit: int = 20,
    concurrency: int = 4,
) -> tuple[dict[str, str], bool, tuple[str, ...]]:
    """Hash bounded edge configurations in memory without retaining raw text."""
    if not 1 <= detail_limit <= 100:
        raise ValueError("detail_limit must be between 1 and 100")
    if not 1 <= concurrency <= 16:
        raise ValueError("concurrency must be between 1 and 16")
    edge_nodes = [node for node in topology.nodes if node.kind == "edge"]
    eligible = [
        node
        for node in edge_nodes
        if node.system_ip is not None and node.device_uuid is not None
    ]
    selected = eligible[:detail_limit]
    semaphore = asyncio.Semaphore(concurrency)

    async def fetch_digest(system_ip: str, device_uuid: str) -> tuple[str, str, str | None]:
        endpoint = f"/dataservice/template/config/running/{device_uuid}"
        source = f"GET {endpoint}"
        try:
            async with semaphore:
                configuration = await client.get_raw(endpoint)
        except Exception:
            return system_ip, source, None
        return system_ip, source, configuration_digest(configuration)

    results = await asyncio.gather(
        *(
            fetch_digest(node.system_ip, node.device_uuid)
            for node in selected
            if node.system_ip is not None and node.device_uuid is not None
        )
    )
    hashes = {
        system_ip: digest
        for system_ip, _source, digest in results
        if digest is not None
    }
    sources = tuple(source for _system_ip, source, _digest in results)
    partial = (
        len(eligible) != len(edge_nodes)
        or len(eligible) > detail_limit
        or any(digest is None for _system_ip, _source, digest in results)
    )
    return hashes, partial, sources


def _as_datetime(value: Any, fallback: datetime) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(UTC)
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1_000
        try:
            return datetime.fromtimestamp(timestamp, tz=UTC)
        except (OSError, OverflowError, ValueError):
            return fallback
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.astimezone(UTC)
        except ValueError:
            return fallback
    return fallback


def build_fabric_snapshot(
    overview: Mapping[str, Any],
    topology: TopologyGraph,
    *,
    sla_samples: Sequence[SlaSample] = (),
    sla_partial: bool = False,
    sla_sources: Sequence[str] = (),
    configuration_hashes: Mapping[str, str] | None = None,
    configuration_partial: bool = False,
    configuration_sources: Sequence[str] = (),
    captured_at: datetime | None = None,
) -> FabricSnapshot:
    """Reduce normalized live evidence into an immutable snapshot."""
    captured = captured_at or datetime.now(UTC)
    overview_time = _as_datetime(overview.get("updated_at"), captured)
    topology_sources = {source.id: topology.generated_at for source in topology.sources}
    overview_sources = {
        str(source.get("label")): overview_time
        for source in overview.get("sources", [])
        if isinstance(source, Mapping) and source.get("label")
    }
    source_freshness = {**topology_sources, **overview_sources}
    source_freshness.update({source: captured for source in sla_sources})
    source_freshness.update({source: captured for source in configuration_sources})
    incomplete_sources = {
        source.id for source in topology.sources if source.state != "ok"
    }
    incomplete_sources.update(
        str(source.get("label"))
        for source in overview.get("sources", [])
        if isinstance(source, Mapping) and source.get("state") == "failed"
    )
    if sla_partial:
        incomplete_sources.update(sla_sources)
    if configuration_partial:
        incomplete_sources.update(configuration_sources or ("configuration-hashes",))

    bfd_edges = [edge for edge in topology.edges if edge.kind == "bfd"]
    control_edges = [edge for edge in topology.edges if edge.kind == "control"]
    snapshot_devices = []
    for node in topology.nodes:
        if node.kind not in {"edge", "controller"}:
            continue
        device_kind: DeviceKind = "edge" if node.kind == "edge" else "controller"
        snapshot_devices.append(
            DeviceSnapshot(
                system_ip=node.system_ip or "unknown",
                hostname=node.label,
                site_id=node.site_id or "unknown",
                kind=device_kind,
                reachable=node.reachable,
                health=node.status,
            )
        )
    devices = tuple(snapshot_devices)
    raw_alarm_counts = overview.get("alarms", {})
    alarm_counts = {
        str(severity): int(count)
        for severity, count in raw_alarm_counts.items()
        if isinstance(raw_alarm_counts, Mapping) and isinstance(count, (int, float))
    }
    health = str(overview.get("health", "unknown"))
    if health not in _HEALTH_WEIGHT:
        health = "unknown"
    normalized_health = cast(HealthStatus, health)
    return FabricSnapshot(
        captured_at=captured,
        health=normalized_health,
        partial=(
            bool(overview.get("partial"))
            or topology.partial
            or sla_partial
            or configuration_partial
        ),
        impact_scope=(
            str(overview.get("impact", {}).get("scope", "unknown"))
            if isinstance(overview.get("impact"), Mapping)
            else "unknown"
        ),
        devices=devices,
        alarm_counts=alarm_counts,
        bfd_up=sum(edge.status == "healthy" for edge in bfd_edges),
        bfd_down=sum(edge.status in {"degraded", "critical"} for edge in bfd_edges),
        control_up=sum(edge.status == "healthy" for edge in control_edges),
        control_down=sum(edge.status in {"degraded", "critical"} for edge in control_edges),
        configuration_hashes=dict(configuration_hashes or {}),
        sla_samples=tuple(sla_samples),
        source_freshness=source_freshness,
        incomplete_sources=tuple(sorted(incomplete_sources)),
    )


def compare_snapshots(before: FabricSnapshot, after: FabricSnapshot) -> SnapshotComparison:
    """Compute a deterministic before/after comparison."""
    before_devices = {device.system_ip: device for device in before.devices}
    after_devices = {device.system_ip: device for device in after.devices}
    device_changes = []
    for system_ip in sorted(before_devices.keys() | after_devices.keys()):
        previous = before_devices.get(system_ip)
        current = after_devices.get(system_ip)
        if previous == current:
            continue
        if previous is None:
            change = "added"
        elif current is None:
            change = "removed"
        else:
            change = "changed"
        selected = current if current is not None else previous
        if selected is None:
            continue
        device_changes.append(
            DeviceChange(
                system_ip=system_ip,
                hostname=selected.hostname,
                change=change,
                reachable={
                    "before": previous.reachable if previous else None,
                    "after": current.reachable if current else None,
                },
                health={
                    "before": previous.health if previous else None,
                    "after": current.health if current else None,
                },
            )
        )

    alarm_delta = {
        severity: after.alarm_counts.get(severity, 0) - before.alarm_counts.get(severity, 0)
        for severity in sorted(before.alarm_counts.keys() | after.alarm_counts.keys())
        if after.alarm_counts.get(severity, 0) != before.alarm_counts.get(severity, 0)
    }
    configuration_changes = sorted(
        system_ip
        for system_ip in before.configuration_hashes.keys() | after.configuration_hashes.keys()
        if before.configuration_hashes.get(system_ip) != after.configuration_hashes.get(system_ip)
    )
    bfd_up_delta = after.bfd_up - before.bfd_up
    bfd_down_delta = after.bfd_down - before.bfd_down
    control_up_delta = after.control_up - before.control_up
    control_down_delta = after.control_down - before.control_down
    before_unreachable = sum(device.reachable is False for device in before.devices)
    after_unreachable = sum(device.reachable is False for device in after.devices)
    regression = any(
        (
            _HEALTH_WEIGHT[after.health] > _HEALTH_WEIGHT[before.health],
            after_unreachable > before_unreachable,
            after.alarm_counts.get("Critical", 0) > before.alarm_counts.get("Critical", 0),
            bfd_down_delta > 0,
            control_down_delta > 0,
        )
    )
    improvement = any(
        (
            _HEALTH_WEIGHT[after.health] < _HEALTH_WEIGHT[before.health],
            after_unreachable < before_unreachable,
            after.alarm_counts.get("Critical", 0) < before.alarm_counts.get("Critical", 0),
            bfd_down_delta < 0,
            control_down_delta < 0,
        )
    )
    verdict: ComparisonVerdict
    if regression and improvement:
        verdict = "mixed"
    elif regression:
        verdict = "regressed"
    elif improvement:
        verdict = "improved"
    else:
        verdict = "unchanged"
    return SnapshotComparison(
        before_id=before.id,
        after_id=after.id,
        before_at=before.captured_at,
        after_at=after.captured_at,
        verdict=verdict,
        health={"before": before.health, "after": after.health},
        device_changes=tuple(device_changes),
        alarm_delta=alarm_delta,
        bfd_up_delta=bfd_up_delta,
        bfd_down_delta=bfd_down_delta,
        control_up_delta=control_up_delta,
        control_down_delta=control_down_delta,
        configuration_changes=configuration_changes,
    )


def _average(values: Sequence[float | None]) -> float | None:
    numeric = [value for value in values if value is not None]
    return round(fmean(numeric), 3) if numeric else None


def build_sla_trends(snapshots: Sequence[FabricSnapshot], *, days: int = 30) -> SlaTrends:
    """Aggregate retained snapshots into chronological fault-domain trends."""
    if not 1 <= days <= 365:
        raise ValueError("days must be between 1 and 365")
    ordered = sorted(snapshots, key=lambda snapshot: snapshot.captured_at)
    if not ordered:
        return SlaTrends(days=days, snapshot_count=0, generated_at=None, series=())
    cutoff = ordered[-1].captured_at - timedelta(days=days)
    retained = [snapshot for snapshot in ordered if snapshot.captured_at >= cutoff]
    points: dict[FaultDomain, list[SlaTrendPoint]] = defaultdict(list)

    for snapshot in retained:
        known_reachability = [
            device.reachable for device in snapshot.devices if device.reachable is not None
        ]
        branch_availability = (
            100 * sum(known_reachability) / len(known_reachability)
            if known_reachability
            else None
        )
        points["branch"].append(
            SlaTrendPoint(
                captured_at=snapshot.captured_at,
                availability_percent=round(branch_availability, 3)
                if branch_availability is not None
                else None,
            )
        )
        samples_by_domain: dict[FaultDomain, list[SlaSample]] = defaultdict(list)
        for sample in snapshot.sla_samples:
            samples_by_domain[sample.fault_domain].append(sample)
        for domain, samples in samples_by_domain.items():
            points[domain].append(
                SlaTrendPoint(
                    captured_at=snapshot.captured_at,
                    latency_ms=_average([sample.latency_ms for sample in samples]),
                    jitter_ms=_average([sample.jitter_ms for sample in samples]),
                    loss_percent=_average([sample.loss_percent for sample in samples]),
                    availability_percent=_average(
                        [sample.availability_percent for sample in samples]
                    ),
                )
            )
    series = tuple(
        SlaTrendSeries(fault_domain=domain, points=tuple(points[domain]))
        for domain in _DOMAIN_ORDER
        if points[domain]
    )
    return SlaTrends(
        days=days,
        snapshot_count=len(retained),
        generated_at=retained[-1].captured_at,
        series=series,
    )


def snapshot_freshness(
    snapshot: FabricSnapshot,
    *,
    stale_after_seconds: int,
    now: datetime | None = None,
) -> SnapshotFreshnessReport:
    """Classify snapshot source ages against an operator-selected threshold."""
    if not 1 <= stale_after_seconds <= 86_400:
        raise ValueError("stale_after_seconds must be between 1 and 86400")
    current_time = now or datetime.now(UTC)
    sources = tuple(
        SourceFreshness(
            source=source,
            observed_at=observed_at,
            age_seconds=max(0, round((current_time - observed_at).total_seconds(), 3)),
            state=(
                "delayed"
                if (current_time - observed_at).total_seconds() > stale_after_seconds
                else "current"
            ),
        )
        for source, observed_at in sorted(snapshot.source_freshness.items())
    )
    return SnapshotFreshnessReport(
        snapshot_id=snapshot.id,
        stale_after_seconds=stale_after_seconds,
        delayed_count=sum(source.state == "delayed" for source in sources),
        sources=sources,
    )
