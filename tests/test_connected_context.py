"""Golden contracts for normalized topology and the actions inbox."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cisco_vmanage_mcp.services.connected_context import (
    build_action_inbox,
    build_topology,
    collect_action_inbox,
    collect_topology,
)

FIXTURES = Path(__file__).with_name("fixtures")
GENERATED_AT = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


def _fixture() -> dict:
    return json.loads((FIXTURES / "connected_context_input.json").read_text())


def test_topology_matches_golden_schema() -> None:
    fixture = _fixture()

    topology = build_topology(
        fixture["devices"],
        fixture["bfd_sessions"],
        fixture["control_connections"],
        generated_at=GENERATED_AT,
    )
    projection = {
        "schema_version": topology.schema_version,
        "summary": topology.summary.model_dump(),
        "node_ids": sorted(node.id for node in topology.nodes),
        "edge_ids": sorted(edge.id for edge in topology.edges),
        "statuses": {node.id: node.status for node in sorted(topology.nodes, key=lambda n: n.id)},
    }

    expected = json.loads((FIXTURES / "connected_context_golden.json").read_text())
    assert projection == expected
    assert all(node.evidence for node in topology.nodes)
    assert all(edge.evidence for edge in topology.edges)


def test_topology_deduplicates_bidirectional_tunnels() -> None:
    fixture = _fixture()
    reverse = {
        "system-ip": "10.0.0.1",
        "state": "up",
        "src-ip": "192.0.2.2",
        "dst-ip": "192.0.2.1",
        "local-color": "mpls",
        "color": "biz-internet",
    }
    fixture["bfd_sessions"]["10.0.0.2"] = [reverse]

    topology = build_topology(
        fixture["devices"],
        fixture["bfd_sessions"],
        fixture["control_connections"],
        generated_at=GENERATED_AT,
    )

    tunnels = [edge for edge in topology.edges if edge.kind == "bfd"]
    assert len(tunnels) == 1
    assert tunnels[0].status == "critical"


def test_topology_normalizes_fifty_sites_and_five_hundred_devices() -> None:
    devices = [
        {
            "host-name": f"edge-{index}",
            "system-ip": f"10.{index // 250}.{(index // 250) + 1}.{(index % 250) + 1}",
            "device-type": "vedge",
            "device-model": "C8000V",
            "reachability": "reachable",
            "site-id": str((index % 50) + 1),
            "state": "green",
        }
        for index in range(500)
    ]

    topology = build_topology(devices, {}, {}, generated_at=GENERATED_AT)

    assert topology.summary.sites == 50
    assert topology.summary.edges == 500
    assert len(topology.nodes) == 550
    assert len(topology.edges) == 500


def test_actions_group_related_evidence_and_prioritize() -> None:
    fixture = _fixture()
    fixture["events"].append(dict(fixture["events"][0]))

    inbox = build_action_inbox(
        fixture["devices"],
        fixture["alarms"],
        fixture["events"],
        generated_at=GENERATED_AT,
    )

    assert [item.priority for item in inbox.items] == ["P1", "P2"]
    first = inbox.items[0]
    assert first.category == "control-plane"
    assert first.site_id == "100"
    assert first.system_ips == ["10.0.0.1"]
    assert first.alarm_count == 1
    assert first.event_count == 2
    assert len(first.evidence) == 2
    assert inbox.summary == {"P1": 1, "P2": 1, "P3": 0, "P4": 0}


@pytest.mark.asyncio
async def test_topology_collection_bounds_detail_and_marks_partial() -> None:
    fixture = _fixture()

    class ContextClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict | None]] = []
            self.active = 0
            self.max_active = 0

        async def get(self, endpoint: str, params=None) -> dict:
            self.calls.append((endpoint, params))
            if endpoint == "/dataservice/device":
                return {"data": fixture["devices"]}
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            await asyncio.sleep(0)
            self.active -= 1
            assert params is not None
            system_ip = params["deviceId"]
            key = "bfd_sessions" if endpoint.endswith("bfd/sessions") else "control_connections"
            return {"data": fixture[key].get(system_ip, [])}

    client = ContextClient()
    topology = await collect_topology(client, detail_limit=1, concurrency=1)

    assert topology.partial is True
    assert len(client.calls) == 3
    assert client.max_active == 1
    assert topology.summary.edges == 2


@pytest.mark.asyncio
async def test_action_collection_preserves_partial_results() -> None:
    fixture = _fixture()

    class PartialClient:
        async def get(self, endpoint: str, params=None) -> dict:
            if endpoint == "/dataservice/device":
                return {"data": fixture["devices"]}
            if endpoint == "/dataservice/alarms":
                return {"data": fixture["alarms"]}
            raise RuntimeError("event source unavailable")

    inbox = await collect_action_inbox(PartialClient())

    assert inbox.partial is True
    assert len(inbox.items) == 1
    states = {source.id: source.state for source in inbox.sources}
    assert states["GET /dataservice/device"] == "ok"
    assert states["GET /dataservice/alarms"] == "ok"
    assert states["GET /dataservice/event"] == "failed"


@pytest.mark.asyncio
async def test_action_collection_bounds_event_history() -> None:
    now_ms = int(time.time() * 1000)

    class EventClient:
        def __init__(self) -> None:
            self.event_params: dict | None = None

        async def get(self, endpoint: str, params=None) -> dict:
            if endpoint == "/dataservice/event":
                self.event_params = params
                return {
                    "data": [
                        {
                            "eventname": "device-reboot",
                            "severity_level": "Major",
                            "system_ip": "10.0.0.1",
                            "entry_time": now_ms,
                        },
                        {
                            "eventname": "device-reboot",
                            "severity_level": "Major",
                            "system_ip": "10.0.0.1",
                            "entry_time": now_ms - (48 * 60 * 60 * 1000),
                        },
                    ]
                }
            return {"data": []}

    client = EventClient()
    inbox = await collect_action_inbox(client, event_hours=24)

    assert client.event_params is not None
    query = json.loads(client.event_params["query"])
    assert query["query"]["rules"][0]["operator"] == "last_n_hours"
    assert query["query"]["rules"][0]["value"] == ["24"]
    assert sum(item.event_count for item in inbox.items) == 1
