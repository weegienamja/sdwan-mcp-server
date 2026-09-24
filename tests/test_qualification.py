"""Customer environment qualification contracts."""

from __future__ import annotations

import asyncio

import pytest

from cisco_vmanage_mcp.client import NotFoundError, PermissionError
from cisco_vmanage_mcp.services.qualification import qualify_environment


class QualificationClient:
    host = "vmanage.customer.example"
    port = "443"

    def __init__(self, failures: dict[str, Exception] | None = None) -> None:
        self.failures = failures or {}
        self.calls: list[tuple[str, dict | None]] = []
        self.active = 0
        self.max_active = 0

    async def get(self, endpoint: str, params=None) -> dict:
        self.calls.append((endpoint, params))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0)
        self.active -= 1
        if endpoint in self.failures:
            raise self.failures[endpoint]
        if endpoint == "/dataservice/device":
            return {
                "data": [
                    {
                        "device-type": "vmanage",
                        "device-model": "vManage",
                        "version": "20.12.4",
                        "system-ip": "10.0.0.10",
                    },
                    {
                        "device-type": "vedge",
                        "device-model": "C8500-12X",
                        "version": "17.12.3",
                        "system-ip": "10.0.0.1",
                    },
                ]
            }
        return {"data": []}


@pytest.mark.asyncio
async def test_qualification_profiles_environment_and_bounds_concurrency() -> None:
    client = QualificationClient()

    report = await qualify_environment(client, concurrency=2)

    assert report.ready is True
    assert report.partial is False
    assert report.manager == "vmanage.customer.example:443"
    assert report.device_count == 2
    assert report.device_types == {"vedge": 1, "vmanage": 1}
    assert report.device_models == {"C8500-12X": 1, "vManage": 1}
    assert report.software_versions == {"17.12.3": 1, "20.12.4": 1}
    assert all(check.status == "available" for check in report.capabilities)
    assert sum(endpoint == "/dataservice/device" for endpoint, _ in client.calls) == 1
    assert client.max_active <= 2
    device_calls = [params for endpoint, params in client.calls if endpoint.startswith("/dataservice/device/")]
    assert all(params == {"deviceId": "10.0.0.1"} for params in device_calls)


@pytest.mark.asyncio
async def test_optional_capability_failures_are_classified_without_failing_readiness() -> None:
    client = QualificationClient({
        "/dataservice/template/device": PermissionError("denied", 403),
        "/dataservice/template/policy/vsmart": NotFoundError("missing", 404),
    })

    report = await qualify_environment(client)
    statuses = {check.id: check.status for check in report.capabilities}

    assert report.ready is True
    assert report.partial is True
    assert statuses["device-templates"] == "forbidden"
    assert statuses["central-policies"] == "unsupported"
    assert report.warnings == (
        "device-templates: permission-denied",
        "central-policies: endpoint-not-found",
    )


@pytest.mark.asyncio
async def test_missing_inventory_fails_readiness_and_skips_device_probes() -> None:
    client = QualificationClient({
        "/dataservice/device": PermissionError("denied", 403),
    })

    report = await qualify_environment(client)
    statuses = {check.id: check.status for check in report.capabilities}

    assert report.ready is False
    assert report.device_count == 0
    assert statuses["inventory"] == "forbidden"
    assert client.calls == [("/dataservice/device", None)]
    assert all(
        statuses[capability] == "not-applicable"
        for capability in (
            "interfaces",
            "counters",
            "bfd",
            "control-connections",
            "omp",
            "tunnel-statistics",
            "application-route-statistics",
            "system-status",
        )
    )


@pytest.mark.asyncio
async def test_empty_inventory_is_not_ready() -> None:
    class EmptyQualificationClient(QualificationClient):
        async def get(self, endpoint: str, params=None) -> dict:
            self.calls.append((endpoint, params))
            return {"data": []}

    report = await qualify_environment(EmptyQualificationClient())

    assert report.ready is False
    assert report.partial is True
    assert "inventory: no-devices" in report.warnings


@pytest.mark.asyncio
async def test_qualification_rejects_unbounded_concurrency() -> None:
    with pytest.raises(ValueError, match="between 1 and 10"):
        await qualify_environment(QualificationClient(), concurrency=11)