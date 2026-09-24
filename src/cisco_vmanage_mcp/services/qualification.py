"""Read-only customer environment qualification for Cisco Catalyst SD-WAN."""

from __future__ import annotations

import asyncio
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict

from cisco_vmanage_mcp.client import (
    AuthenticationError,
    ConnectionError,
    NotFoundError,
    RateLimitError,
    TimeoutError,
    VManageAPIError,
)
from cisco_vmanage_mcp.client import (
    PermissionError as VManagePermissionError,
)

QUALIFICATION_SCHEMA_VERSION = 1

CapabilityStatus = Literal[
    "available",
    "forbidden",
    "unsupported",
    "unavailable",
    "not-applicable",
]


class QualificationClient(Protocol):
    """Read-only client operations needed by environment qualification."""

    host: str
    port: str

    async def get(self, endpoint: str, params: dict | None = None) -> dict: ...


class CapabilityCheck(BaseModel):
    """Result of probing one bounded read-only API capability."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    endpoint: str
    required: bool
    status: CapabilityStatus
    duration_ms: float = 0.0
    row_count: int | None = None
    reason: str | None = None


class EnvironmentQualification(BaseModel):
    """Sanitized compatibility report for one vManage environment."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = QUALIFICATION_SCHEMA_VERSION
    generated_at: datetime
    manager: str
    ready: bool
    partial: bool
    device_count: int
    device_types: dict[str, int]
    device_models: dict[str, int]
    software_versions: dict[str, int]
    capabilities: tuple[CapabilityCheck, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class _CapabilitySpec:
    id: str
    endpoint: str
    required: bool = False
    device_scoped: bool = False


_GLOBAL_CAPABILITIES = (
    _CapabilitySpec("alarms", "/dataservice/alarms/count", required=True),
    _CapabilitySpec("device-templates", "/dataservice/template/device"),
    _CapabilitySpec("central-policies", "/dataservice/template/policy/vsmart"),
)

_DEVICE_CAPABILITIES = (
    _CapabilitySpec("interfaces", "/dataservice/device/interface", device_scoped=True),
    _CapabilitySpec("counters", "/dataservice/device/counters", device_scoped=True),
    _CapabilitySpec("bfd", "/dataservice/device/bfd/sessions", device_scoped=True),
    _CapabilitySpec(
        "control-connections",
        "/dataservice/device/control/connections",
        device_scoped=True,
    ),
    _CapabilitySpec("omp", "/dataservice/device/omp/peers", device_scoped=True),
    _CapabilitySpec("tunnel-statistics", "/dataservice/device/tunnel/statistics", device_scoped=True),
    _CapabilitySpec(
        "application-route-statistics",
        "/dataservice/device/app-route/statistics",
        device_scoped=True,
    ),
    _CapabilitySpec("system-status", "/dataservice/device/system/status", device_scoped=True),
)


def _rows(payload: Mapping[str, Any]) -> Sequence[Any]:
    data = payload.get("data", [])
    return data if isinstance(data, list) else []


def _failure_status(exc: Exception) -> tuple[CapabilityStatus, str]:
    if isinstance(exc, AuthenticationError):
        return "forbidden", "authentication-failed"
    if isinstance(exc, VManagePermissionError):
        return "forbidden", "permission-denied"
    if isinstance(exc, NotFoundError):
        return "unsupported", "endpoint-not-found"
    if isinstance(exc, RateLimitError):
        return "unavailable", "rate-limited"
    if isinstance(exc, TimeoutError):
        return "unavailable", "timeout"
    if isinstance(exc, ConnectionError):
        return "unavailable", "connection-failed"
    if isinstance(exc, VManageAPIError):
        return "unavailable", "api-error"
    return "unavailable", type(exc).__name__


def _field_value(device: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = device.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return "unknown"


def _build_report(
    client: QualificationClient,
    devices: Sequence[Mapping[str, Any]],
    capabilities: tuple[CapabilityCheck, ...],
) -> EnvironmentQualification:
    required_ready = all(
        capability.status == "available"
        for capability in capabilities
        if capability.required
    )
    warnings = [
        f"{capability.id}: {capability.reason}"
        for capability in capabilities
        if capability.status not in {"available", "not-applicable"}
    ]
    if not devices:
        warnings.append("inventory: no-devices")
    return EnvironmentQualification(
        generated_at=datetime.now(UTC),
        manager=f"{client.host}:{client.port}",
        ready=required_ready and bool(devices),
        partial=bool(warnings),
        device_count=len(devices),
        device_types=dict(sorted(Counter(
            _field_value(device, "device-type", "device_type")
            for device in devices
        ).items())),
        device_models=dict(sorted(Counter(
            _field_value(device, "device-model", "device_model")
            for device in devices
        ).items())),
        software_versions=dict(sorted(Counter(
            _field_value(device, "version") for device in devices
        ).items())),
        capabilities=capabilities,
        warnings=tuple(warnings),
    )


async def _probe_capability(
    client: QualificationClient,
    spec: _CapabilitySpec,
    semaphore: asyncio.Semaphore,
    *,
    system_ip: str | None = None,
) -> CapabilityCheck:
    if spec.device_scoped and system_ip is None:
        return CapabilityCheck(
            id=spec.id,
            endpoint=spec.endpoint,
            required=spec.required,
            status="not-applicable",
            reason="no-edge-device",
        )
    params = {"deviceId": system_ip} if system_ip is not None else None
    started = time.monotonic()
    try:
        async with semaphore:
            payload = await client.get(spec.endpoint, params=params)
        duration_ms = (time.monotonic() - started) * 1_000
        return CapabilityCheck(
            id=spec.id,
            endpoint=spec.endpoint,
            required=spec.required,
            status="available",
            duration_ms=round(duration_ms, 1),
            row_count=len(_rows(payload)),
        )
    except Exception as exc:
        status, reason = _failure_status(exc)
        return CapabilityCheck(
            id=spec.id,
            endpoint=spec.endpoint,
            required=spec.required,
            status=status,
            duration_ms=round((time.monotonic() - started) * 1_000, 1),
            reason=reason,
        )


async def _probe_inventory(
    client: QualificationClient,
    semaphore: asyncio.Semaphore,
) -> tuple[CapabilityCheck, list[Mapping[str, Any]]]:
    spec = _CapabilitySpec("inventory", "/dataservice/device", required=True)
    started = time.monotonic()
    try:
        async with semaphore:
            payload = await client.get(spec.endpoint)
        devices = [item for item in _rows(payload) if isinstance(item, Mapping)]
        return (
            CapabilityCheck(
                id=spec.id,
                endpoint=spec.endpoint,
                required=True,
                status="available",
                duration_ms=round((time.monotonic() - started) * 1_000, 1),
                row_count=len(devices),
            ),
            devices,
        )
    except Exception as exc:
        status, reason = _failure_status(exc)
        return (
            CapabilityCheck(
                id=spec.id,
                endpoint=spec.endpoint,
                required=True,
                status=status,
                duration_ms=round((time.monotonic() - started) * 1_000, 1),
                reason=reason,
            ),
            [],
        )


async def qualify_environment(
    client: QualificationClient,
    *,
    concurrency: int = 3,
) -> EnvironmentQualification:
    """Probe supported read-only APIs and summarize the customer environment."""
    if not 1 <= concurrency <= 10:
        raise ValueError("concurrency must be between 1 and 10")
    semaphore = asyncio.Semaphore(concurrency)
    inventory, devices = await _probe_inventory(client, semaphore)

    if inventory.status != "available":
        skipped = tuple(
            CapabilityCheck(
                id=spec.id,
                endpoint=spec.endpoint,
                required=spec.required,
                status="not-applicable",
                reason="inventory-unavailable",
            )
            for spec in (*_GLOBAL_CAPABILITIES, *_DEVICE_CAPABILITIES)
        )
        return _build_report(client, devices, (inventory, *skipped))

    edge = next(
        (
            device
            for device in devices
            if str(device.get("device-type", device.get("device_type", ""))).lower()
            == "vedge"
            and str(device.get("reachability", "")).lower() == "reachable"
        ),
        next(
            (
                device
                for device in devices
                if str(device.get("device-type", device.get("device_type", ""))).lower()
                == "vedge"
            ),
            None,
        ),
    )
    system_ip = _field_value(edge, "system-ip", "system_ip") if edge is not None else None
    if system_ip == "unknown":
        system_ip = None
    checks = await asyncio.gather(
        *(
            _probe_capability(client, spec, semaphore)
            for spec in _GLOBAL_CAPABILITIES
        ),
        *(
            _probe_capability(client, spec, semaphore, system_ip=system_ip)
            for spec in _DEVICE_CAPABILITIES
        ),
    )
    capabilities = (inventory, *checks)
    return _build_report(client, devices, capabilities)