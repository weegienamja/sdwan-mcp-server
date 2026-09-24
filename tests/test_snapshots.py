"""Contracts for historical fabric snapshots and deterministic comparisons."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest

from cisco_vmanage_mcp.services.connected_context import build_topology
from cisco_vmanage_mcp.services.snapshots import (
    DeviceSnapshot,
    FabricSnapshot,
    SlaSample,
    SnapshotStore,
    SnapshotStoreError,
    build_fabric_snapshot,
    build_sla_trends,
    classify_fault_domain,
    collect_configuration_hashes,
    collect_sla_samples,
    compare_snapshots,
    configuration_digest,
    snapshot_freshness,
)

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


def _snapshot(
    snapshot_id: str,
    captured_at: datetime,
    *,
    reachable: bool = True,
    critical_alarms: int = 0,
    bfd_down: int = 0,
    config_hash: str = "aaa",
    latency_ms: float = 20,
) -> FabricSnapshot:
    return FabricSnapshot(
        id=snapshot_id,
        captured_at=captured_at,
        health="healthy" if reachable and not critical_alarms else "critical",
        partial=False,
        devices=(
            DeviceSnapshot(
                system_ip="10.0.0.1",
                hostname="edge-1",
                site_id="100",
                kind="edge",
                reachable=reachable,
                health="healthy" if reachable else "critical",
            ),
        ),
        alarm_counts={"Critical": critical_alarms},
        bfd_up=1 - bfd_down,
        bfd_down=bfd_down,
        control_up=1,
        control_down=0,
        configuration_hashes={"10.0.0.1": config_hash},
        sla_samples=(
            SlaSample(
                system_ip="10.0.0.1",
                peer_system_ip="10.0.0.2",
                site_id="100",
                fault_domain="internet",
                local_color="biz-internet",
                remote_color="biz-internet",
                state="up",
                latency_ms=latency_ms,
                jitter_ms=3,
                loss_percent=0.1,
                availability_percent=100,
                source="GET /dataservice/device/tunnel/statistics?deviceId=10.0.0.1",
            ),
        ),
        source_freshness={"GET /dataservice/device": captured_at},
    )


def test_snapshot_store_encrypts_prunes_and_uses_private_permissions(tmp_path) -> None:
    store = SnapshotStore(tmp_path / "state", retention_days=30, max_snapshots=2)
    old = _snapshot("old", NOW - timedelta(days=31))
    first = _snapshot("first", NOW - timedelta(hours=1))
    second = _snapshot("second", NOW)

    store.add(old)
    store.add(first)
    store.add(second)

    assert [snapshot.id for snapshot in store.list()] == ["second", "first"]
    assert store.get("old") is None
    assert b"edge-1" not in store.database_path.read_bytes()
    assert store.database_path.stat().st_mode & 0o777 == 0o600
    assert store.key_path.stat().st_mode & 0o777 == 0o600
    assert store.state_dir.stat().st_mode & 0o777 == 0o700


def test_snapshot_store_quarantines_corrupt_payload(tmp_path) -> None:
    store = SnapshotStore(tmp_path / "state")
    store.add(_snapshot("corrupt-me", NOW))
    with closing(sqlite3.connect(store.database_path)) as connection:
        with connection:
            connection.execute(
                "UPDATE snapshots SET payload = ? WHERE id = ?",
                (b"not-a-valid-token", "corrupt-me"),
            )

    assert store.get("corrupt-me") is None
    assert store.quarantined_count() == 1
    assert store.list() == []


def test_snapshot_store_rejects_newer_schema(tmp_path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    database = state_dir / "snapshots.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA user_version = 99")

    with pytest.raises(SnapshotStoreError, match="newer than supported"):
        SnapshotStore(state_dir)


def test_snapshot_comparison_reports_regression_and_hash_change() -> None:
    before = _snapshot("before", NOW - timedelta(hours=1))
    after = _snapshot(
        "after",
        NOW,
        reachable=False,
        critical_alarms=2,
        bfd_down=1,
        config_hash="bbb",
        latency_ms=80,
    )

    comparison = compare_snapshots(before, after)

    assert comparison.verdict == "regressed"
    assert comparison.device_changes[0].system_ip == "10.0.0.1"
    assert comparison.device_changes[0].reachable == {"before": True, "after": False}
    assert comparison.alarm_delta == {"Critical": 2}
    assert comparison.bfd_down_delta == 1
    assert comparison.configuration_changes == ["10.0.0.1"]


def test_sla_trends_include_branch_and_transport_fault_domains() -> None:
    snapshots = [
        _snapshot("first", NOW - timedelta(days=1), latency_ms=20),
        _snapshot("second", NOW, latency_ms=40),
    ]

    trends = build_sla_trends(snapshots, days=30)

    domains = {series.fault_domain: series for series in trends.series}
    assert set(domains) == {"branch", "internet"}
    assert [point.latency_ms for point in domains["internet"].points] == [20, 40]
    assert all(point.availability_percent == 100 for point in domains["branch"].points)
    assert classify_fault_domain("mpls", "mpls") == "wan"
    assert classify_fault_domain("lte", "public-internet") == "isp"
    assert classify_fault_domain("cloud", "cloud") == "cloud"


def test_sla_trends_retain_thirty_day_fixture() -> None:
    snapshots = [
        _snapshot(
            f"day-{day}",
            NOW - timedelta(days=29 - day),
            latency_ms=10 + day,
        )
        for day in range(30)
    ]

    trends = build_sla_trends(snapshots, days=30)
    internet = next(series for series in trends.series if series.fault_domain == "internet")

    assert trends.snapshot_count == 30
    assert len(internet.points) == 30
    assert internet.points[0].latency_ms == 10
    assert internet.points[-1].latency_ms == 39


@pytest.mark.asyncio
async def test_sla_collection_normalizes_tunnel_metrics() -> None:
    topology = build_topology(
        [
            {
                "host-name": "edge-1",
                "system-ip": "10.0.0.1",
                "device-type": "vedge",
                "reachability": "reachable",
                "site-id": "100",
                "state": "green",
            }
        ],
        {},
        {},
        generated_at=NOW,
    )

    class TunnelClient:
        async def get(self, endpoint: str, params=None) -> dict:
            assert endpoint == "/dataservice/device/app-route/statistics"
            assert params == {"deviceId": "10.0.0.1"}
            return {
                "data": [
                    {
                        "remote-system-ip": "10.0.0.2",
                        "local-color": "biz-internet",
                        "remote-color": "biz-internet",
                        "average-latency": "24.5",
                        "average-jitter": 3,
                        "loss": "0.2",
                    }
                ]
            }

    samples, partial, sources = await collect_sla_samples(TunnelClient(), topology)

    assert partial is False
    assert len(samples) == 1
    assert samples[0].fault_domain == "internet"
    assert samples[0].latency_ms == 24.5
    assert samples[0].availability_percent == 99.8
    assert sources == ("GET /dataservice/device/app-route/statistics?deviceId=10.0.0.1",)


@pytest.mark.asyncio
async def test_configuration_collection_hashes_raw_data_and_is_bounded() -> None:
    topology = build_topology(
        [
            {
                "host-name": "edge-1",
                "system-ip": "10.0.0.1",
                "device-type": "vedge",
                "reachability": "reachable",
                "site-id": "100",
                "state": "green",
                "uuid": "edge-uuid-1",
            },
            {
                "host-name": "edge-2",
                "system-ip": "10.0.0.2",
                "device-type": "vedge",
                "reachability": "reachable",
                "site-id": "200",
                "state": "green",
                "uuid": "edge-uuid-2",
            },
        ],
        {},
        {},
        generated_at=NOW,
    )

    class ConfigClient:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def get_raw(self, endpoint: str, params=None) -> str:
            self.calls.append(endpoint)
            return "hostname edge-1\nusername admin password forbidden"

    client = ConfigClient()
    hashes, partial, sources = await collect_configuration_hashes(
        client,
        topology,
        detail_limit=1,
    )

    assert len(client.calls) == 1
    assert hashes == {
        "10.0.0.1": configuration_digest(
            "hostname edge-1\nusername admin password forbidden"
        )
    }
    assert partial is True
    assert sources == (
        "GET /dataservice/template/config/running/edge-uuid-1",
    )
    assert "forbidden" not in json.dumps(hashes)


def test_snapshot_builder_reduces_normalized_evidence() -> None:
    topology = build_topology(
        [
            {
                "host-name": "edge-1",
                "system-ip": "10.0.0.1",
                "device-type": "vedge",
                "reachability": "unreachable",
                "site-id": "100",
                "state": "red",
            }
        ],
        {"10.0.0.1": [{"system-ip": "10.0.0.2", "state": "down"}]},
        {},
        generated_at=NOW,
    )
    overview = {
        "health": "critical",
        "partial": False,
        "alarms": {"Critical": 2},
        "sources": [{"label": "GET /dataservice/device", "state": "ok"}],
        "updated_at": NOW.isoformat(),
    }

    snapshot = build_fabric_snapshot(
        overview,
        topology,
        captured_at=NOW,
        configuration_hashes={"10.0.0.1": configuration_digest("hostname edge-1")},
    )

    assert snapshot.devices[0].reachable is False
    assert snapshot.bfd_down == 1
    assert snapshot.alarm_counts == {"Critical": 2}
    assert len(snapshot.configuration_hashes["10.0.0.1"]) == 64
    assert snapshot.source_freshness["GET /dataservice/device"] == NOW


def test_snapshot_freshness_uses_operator_threshold() -> None:
    snapshot = _snapshot("freshness", NOW).model_copy(
        update={
            "source_freshness": {
                "GET /dataservice/device": NOW,
                "GET /dataservice/alarms": NOW - timedelta(seconds=301),
            }
        }
    )

    report = snapshot_freshness(
        snapshot,
        now=NOW + timedelta(seconds=10),
        stale_after_seconds=300,
    )

    states = {source.source: source.state for source in report.sources}
    assert states == {
        "GET /dataservice/alarms": "delayed",
        "GET /dataservice/device": "current",
    }
    assert report.delayed_count == 1
