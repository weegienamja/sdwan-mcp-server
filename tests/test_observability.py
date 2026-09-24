"""Contracts for opt-in OpenTelemetry snapshot metrics."""

from __future__ import annotations

import json

from cisco_vmanage_mcp.services.observability import (
    export_snapshot_metrics,
    snapshot_metric_points,
)
from tests.test_snapshots import NOW, _snapshot


class RecordingSink:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.points = []

    def export(self, points) -> bool:
        self.points = list(points)
        if self.fail:
            raise RuntimeError("collector unavailable")
        return True


def test_snapshot_metrics_are_operational_and_exclude_device_identity() -> None:
    snapshot = _snapshot("metrics", NOW).model_copy(
        update={"impact_scope": "site"}
    )

    points = snapshot_metric_points(snapshot)
    serialized = json.dumps([point.model_dump() for point in points])

    assert {point.name for point in points} >= {
        "vmanage.device.reachability",
        "vmanage.alarm.count",
        "vmanage.bfd.sessions",
        "vmanage.control.connections",
        "vmanage.sla.latency",
        "vmanage.sla.jitter",
        "vmanage.sla.loss",
        "vmanage.sla.availability",
    }
    assert "10.0.0.1" not in serialized
    assert "edge-1" not in serialized
    assert all("impact_scope" in point.attributes for point in points)


def test_snapshot_export_is_explicit_opt_in(monkeypatch) -> None:
    snapshot = _snapshot("metrics", NOW)
    sink = RecordingSink()

    monkeypatch.delenv("VMANAGE_OTEL_ENABLED", raising=False)
    assert export_snapshot_metrics(snapshot, sink=sink) is False
    assert sink.points == []

    monkeypatch.setenv("VMANAGE_OTEL_ENABLED", "true")
    assert export_snapshot_metrics(snapshot, sink=sink) is True
    assert sink.points


def test_snapshot_export_failure_never_breaks_capture(monkeypatch) -> None:
    monkeypatch.setenv("VMANAGE_OTEL_ENABLED", "true")

    assert export_snapshot_metrics(
        _snapshot("metrics", NOW),
        sink=RecordingSink(fail=True),
    ) is False
