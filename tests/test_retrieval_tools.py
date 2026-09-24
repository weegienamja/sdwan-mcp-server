"""Behavior contracts for every retrieval MCP tool."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from cisco_vmanage_mcp.tools import (
    alarm_tools,
    config_tools,
    device_tools,
    health_tools,
    policy_tools,
    tunnel_tools,
)

NOW_MS = int(time.time() * 1000)
DEVICE = {
    "host-name": "edge-1",
    "system-ip": "10.0.0.1",
    "deviceId": "10.0.0.1",
    "device-type": "vedge",
    "device-model": "vedge-C8000V",
    "reachability": "reachable",
    "version": "20.10.1",
    "site-id": "100",
    "uuid": "device-uuid",
    "state": "green",
    "state_description": "In Sync",
    "bfdSessions": 8,
    "controlConnections": 3,
    "uptime-date": NOW_MS - 60_000,
}

RESPONSES = {
    "/dataservice/device": {"data": [DEVICE]},
    "/dataservice/device/counters": {
        "data": [{"rebootCount": 1, "crashCount": 0, "number-vsmart-control-connections": 3}]
    },
    "/dataservice/device/interface": {
        "data": [{
            "ifname": "GigabitEthernet1",
            "if-admin-status": "Up",
            "if-oper-status": "Up",
            "ip-address": "192.0.2.1/30",
            "speed-mbps": 1000,
            "tx-octets": 100,
            "rx-octets": 200,
        }]
    },
    "/dataservice/device/tunnel/statistics": {
        "data": [{
            "dest-ip": "192.0.2.2",
            "system-ip": "10.0.0.2",
            "tunnel-protocol": "ipsec",
            "source-ip": "192.0.2.1",
            "local-color": "biz-internet",
            "remote-color": "mpls",
            "tx_pkts": 10,
            "rx_pkts": 11,
            "tunnel-mtu": 1442,
        }]
    },
    "/dataservice/device/bfd/sessions": {
        "data": [{
            "system-ip": "10.0.0.2",
            "state": "up",
            "src-ip": "192.0.2.1",
            "src-port": 12346,
            "dst-ip": "192.0.2.2",
            "dst-port": 12346,
            "site-id": "200",
            "local-color": "biz-internet",
            "color": "mpls",
        }]
    },
    "/dataservice/device/omp/peers": {
        "data": [{
            "peer": "10.0.0.10",
            "state": "up",
            "site-id": "100",
            "domain-id": "1",
            "type": "vsmart",
        }]
    },
    "/dataservice/alarms": {
        "data": [{
            "severity": "Critical",
            "type": "Control",
            "host-name": "edge-1",
            "system-ip": "10.0.0.1",
            "active-time": "1m",
            "entry_time": NOW_MS,
            "message": "Control connection changed",
        }]
    },
    "/dataservice/alarms/count": {
        "data": [{"severity": "Critical", "count": 1}]
    },
    "/dataservice/event": {
        "data": [{
            "eventname": "device-reboot",
            "severity": "Major",
            "host-name": "edge-1",
            "system-ip": "10.0.0.1",
            "entry_time": NOW_MS,
        }]
    },
    "/dataservice/template/policy/vsmart": {
        "data": [{
            "policyName": "central-policy",
            "policyDescription": "Production policy",
            "policyType": "feature",
            "isPolicyActivated": True,
            "policyId": "policy-1",
        }]
    },
    "/dataservice/template/device": {
        "data": [{
            "templateName": "edge-template",
            "templateDescription": "WAN edge",
            "deviceType": "vedge-C8000V",
            "devicesAttached": 1,
            "templateId": "template-1",
        }]
    },
    "/dataservice/device/system/status": {
        "data": [{
            "min1_avg": 10,
            "min5_avg": 15,
            "min15_avg": 12,
            "mem_used": 100,
            "mem_free": 900,
            "disk_used": 20,
            "disk_avail": 80,
            "uptime": 3600,
        }]
    },
    "/dataservice/device/control/connections": {
        "data": [{
            "peer-type": "vsmart",
            "system-ip": "10.0.0.10",
            "state": "up",
            "uptime": "1:00:00:00",
        }]
    },
}


class RecordingClient:
    def __init__(self) -> None:
        self.operations: list[tuple[str, str]] = []

    async def get(self, endpoint: str, params=None) -> dict:
        self.operations.append(("GET", endpoint))
        return RESPONSES[endpoint]

    async def get_raw(self, endpoint: str, params=None) -> str:
        self.operations.append(("GET_RAW", endpoint))
        return "hostname edge-1\n!"


def _context(client: RecordingClient):
    return SimpleNamespace(
        request_context=SimpleNamespace(lifespan_context={"vmanage": client})
    )


TOOL_CASES = [
    (device_tools.vmanage_list_devices, {}, "devices"),
    (device_tools.vmanage_get_device_status, {"system_ip": "10.0.0.1"}, "hostname"),
    (device_tools.vmanage_get_device_counters, {"system_ip": "10.0.0.1"}, "counters"),
    (device_tools.vmanage_get_device_interfaces, {"system_ip": "10.0.0.1"}, "interfaces"),
    (tunnel_tools.vmanage_list_tunnels, {"system_ip": "10.0.0.1"}, "tunnels"),
    (tunnel_tools.vmanage_get_bfd_sessions, {"system_ip": "10.0.0.1"}, "sessions"),
    (tunnel_tools.vmanage_get_omp_peers, {"system_ip": "10.0.0.1"}, "peers"),
    (alarm_tools.vmanage_list_alarms, {}, "alarms"),
    (alarm_tools.vmanage_get_alarm_count, {}, "alarm_counts"),
    (alarm_tools.vmanage_list_events, {}, "events"),
    (policy_tools.vmanage_list_policies, {}, "policies"),
    (policy_tools.vmanage_list_templates, {}, "templates"),
    (config_tools.vmanage_get_running_config, {"device_uuid": "device-uuid"}, "config"),
    (health_tools.vmanage_get_system_status, {"system_ip": "10.0.0.1"}, "status"),
    (health_tools.vmanage_get_control_status, {"system_ip": "10.0.0.1"}, "connections"),
    (health_tools.vmanage_get_fabric_summary, {}, "controllers"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("tool", "arguments", "json_key"), TOOL_CASES)
@pytest.mark.parametrize("response_format", ["json", "markdown"])
async def test_retrieval_tool_formats_and_read_only_calls(
    tool,
    arguments,
    json_key,
    response_format,
) -> None:
    client = RecordingClient()

    result = await tool(
        ctx=_context(client),
        response_format=response_format,
        **arguments,
    )

    assert result
    assert not result.startswith("Error:")
    if response_format == "json":
        assert json_key in json.loads(result)
    assert client.operations
    assert {operation for operation, _endpoint in client.operations} <= {"GET", "GET_RAW"}


@pytest.mark.asyncio
async def test_event_device_filter_is_applied_locally() -> None:
    captured: dict = {}

    class EventClient:
        async def get(self, endpoint: str, params=None) -> dict:
            assert endpoint == "/dataservice/event"
            captured["params"] = params
            return {
                "data": [
                    {
                        "system_ip": "10.0.0.1",
                        "host_name": "edge-1",
                        "severity_level": "major",
                        "eventname": "control-change",
                        "entry_time": NOW_MS,
                    },
                    {"system_ip": "10.0.0.2", "entry_time": NOW_MS},
                ]
            }

    context = SimpleNamespace(
        request_context=SimpleNamespace(
            lifespan_context={"vmanage": EventClient()}
        )
    )

    result = await alarm_tools.vmanage_list_events(
        ctx=context,
        system_ip="10.0.0.1",
        response_format="json",
    )

    payload = json.loads(result)
    query = json.loads(captured["params"]["query"])
    rules = query["query"]["rules"]
    assert any(rule["field"] == "entry_time" for rule in rules)
    assert any(
        rule["field"] == "system_ip" and rule["value"] == ["10.0.0.1"]
        for rule in rules
    )
    assert payload["count"] == 1
    assert payload["events"][0]["system_ip"] == "10.0.0.1"
    assert payload["events"][0]["hostname"] == "edge-1"
    assert payload["events"][0]["severity"] == "major"


def test_event_query_can_be_disabled() -> None:
    assert alarm_tools._build_event_query(0, None) is None
