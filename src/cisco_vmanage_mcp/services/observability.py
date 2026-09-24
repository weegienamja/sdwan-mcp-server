"""Optional OpenTelemetry export for privacy-bounded snapshot metrics."""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from collections.abc import Sequence
from statistics import fmean
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from cisco_vmanage_mcp.services.snapshots import FabricSnapshot

logger = logging.getLogger("cisco_vmanage_mcp.observability")


class MetricPoint(BaseModel):
    """One numeric metric with non-identifying operational dimensions."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    value: float
    unit: str
    attributes: dict[str, str]


class MetricSink(Protocol):
    """Destination contract for snapshot metric points."""

    def export(self, points: Sequence[MetricPoint]) -> bool: ...


class OpenTelemetryMetricSink:
    """Export metrics with the optional official OTLP HTTP SDK."""

    def export(self, points: Sequence[MetricPoint]) -> bool:
        """Create gauges, flush them to OTLP, and release exporter resources."""
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import (  # type: ignore[import-not-found]
            OTLPMetricExporter,
        )
        from opentelemetry.sdk.metrics import MeterProvider  # type: ignore[import-not-found]
        from opentelemetry.sdk.metrics.export import (  # type: ignore[import-not-found]
            PeriodicExportingMetricReader,
        )

        exporter = OTLPMetricExporter()
        reader = PeriodicExportingMetricReader(exporter, export_interval_millis=60_000)
        provider = MeterProvider(metric_readers=[reader])
        try:
            meter = provider.get_meter("cisco-vmanage-mcp")
            instruments = {}
            for point in points:
                instrument = instruments.get(point.name)
                if instrument is None:
                    instrument = meter.create_gauge(point.name, unit=point.unit)
                    instruments[point.name] = instrument
                instrument.set(point.value, attributes=point.attributes)
            return bool(provider.force_flush(timeout_millis=_timeout_ms()))
        finally:
            provider.shutdown()


def _timeout_ms() -> int:
    try:
        timeout = int(os.getenv("VMANAGE_OTEL_TIMEOUT_MS", "5000"))
    except ValueError:
        return 5_000
    return min(max(timeout, 100), 30_000)


def _attributes(snapshot: FabricSnapshot, **values: str) -> dict[str, str]:
    return {
        "impact_scope": snapshot.impact_scope,
        "snapshot_health": snapshot.health,
        **values,
    }


def _reachability_points(snapshot: FabricSnapshot) -> list[MetricPoint]:
    reachability: dict[tuple[str, str], list[bool]] = defaultdict(list)
    for device in snapshot.devices:
        if device.reachable is not None:
            reachability[(device.site_id, device.kind)].append(device.reachable)
    return [
        MetricPoint(
            name="vmanage.device.reachability",
            value=100 * sum(values) / len(values),
            unit="%",
            attributes=_attributes(
                snapshot,
                site_id=site_id,
                device_type=device_type,
            ),
        )
        for (site_id, device_type), values in sorted(reachability.items())
    ]


def _alarm_points(snapshot: FabricSnapshot) -> list[MetricPoint]:
    return [
        MetricPoint(
            name="vmanage.alarm.count",
            value=float(count),
            unit="{alarm}",
            attributes=_attributes(snapshot, severity=severity.lower()),
        )
        for severity, count in sorted(snapshot.alarm_counts.items())
    ]


def _state_points(
    snapshot: FabricSnapshot,
    *,
    name: str,
    unit: str,
    states: tuple[tuple[str, int], ...],
) -> list[MetricPoint]:
    return [
        MetricPoint(
            name=name,
            value=float(count),
            unit=unit,
            attributes=_attributes(snapshot, state=state),
        )
        for state, count in states
    ]


def _sla_points(snapshot: FabricSnapshot) -> list[MetricPoint]:
    points: list[MetricPoint] = []
    grouped_samples: dict[tuple[str, str, str, str], list] = defaultdict(list)
    for sample in snapshot.sla_samples:
        grouped_samples[
            (sample.site_id, sample.fault_domain, sample.local_color, sample.remote_color)
        ].append(sample)
    metric_fields = (
        ("vmanage.sla.latency", "latency_ms", "ms"),
        ("vmanage.sla.jitter", "jitter_ms", "ms"),
        ("vmanage.sla.loss", "loss_percent", "%"),
        ("vmanage.sla.availability", "availability_percent", "%"),
    )
    for dimensions, samples in sorted(grouped_samples.items()):
        site_id, fault_domain, local_color, remote_color = dimensions
        attributes = _attributes(
            snapshot,
            site_id=site_id,
            fault_domain=fault_domain,
            local_color=local_color,
            remote_color=remote_color,
        )
        for name, field, unit in metric_fields:
            values = [getattr(sample, field) for sample in samples if getattr(sample, field) is not None]
            if values:
                points.append(
                    MetricPoint(
                        name=name,
                        value=fmean(values),
                        unit=unit,
                        attributes=attributes,
                    )
                )
    return points


def snapshot_metric_points(snapshot: FabricSnapshot) -> tuple[MetricPoint, ...]:
    """Project a snapshot into metrics without hostnames or system IPs."""
    points = _reachability_points(snapshot)
    points.extend(_alarm_points(snapshot))
    points.extend(
        _state_points(
            snapshot,
            name="vmanage.bfd.sessions",
            unit="{session}",
            states=(("up", snapshot.bfd_up), ("down", snapshot.bfd_down)),
        )
    )
    points.extend(
        _state_points(
            snapshot,
            name="vmanage.control.connections",
            unit="{connection}",
            states=(("up", snapshot.control_up), ("down", snapshot.control_down)),
        )
    )
    points.extend(_sla_points(snapshot))
    return tuple(points)


def export_snapshot_metrics(
    snapshot: FabricSnapshot,
    *,
    sink: MetricSink | None = None,
) -> bool:
    """Export snapshot metrics when explicitly enabled, never affecting capture."""
    if os.getenv("VMANAGE_OTEL_ENABLED", "false").lower() != "true":
        return False
    try:
        return (sink or OpenTelemetryMetricSink()).export(snapshot_metric_points(snapshot))
    except Exception as exc:
        logger.warning("OpenTelemetry snapshot export failed: %s", type(exc).__name__)
        return False
