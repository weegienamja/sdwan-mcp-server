"""Run a sanitized read-only audit of every MCP tool against configured vManage."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CallToolResult, TextContent

EXPECTED_TOOL_COUNT = 21


def _result_text(result: CallToolResult) -> str:
    return "\n".join(
        item.text
        for item in result.content
        if isinstance(item, TextContent)
    )


async def _call(
    session: ClientSession,
    name: str,
    arguments: dict[str, Any],
    deadline_seconds: float = 90.0,
) -> tuple[bool, str]:
    try:
        result = await asyncio.wait_for(
            session.call_tool(name, arguments=arguments),
            timeout=deadline_seconds,
        )
    except TimeoutError:
        return False, "timeout"
    except Exception as exc:
        return False, type(exc).__name__

    text = _result_text(result)
    if result.isError or text.startswith("Error:"):
        return False, "tool error"
    return True, text


async def audit() -> int:
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "cisco_vmanage_mcp"],
        env=dict(os.environ),
    )

    async with stdio_client(parameters) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            initialized = await session.initialize()
            tools = (await session.list_tools()).tools
            print(f"server_version={initialized.serverInfo.version}")
            print(f"tool_count={len(tools)}")
            if len(tools) != EXPECTED_TOOL_COUNT:
                print("FAIL registry")
                return 1
            if any(
                tool.annotations is None
                or tool.annotations.readOnlyHint is not True
                or tool.annotations.destructiveHint is not False
                for tool in tools
            ):
                print("FAIL annotations")
                return 1

            passed = 0
            failed = 0
            ok, device_text = await _call(
                session,
                "vmanage_list_devices",
                {"response_format": "json", "limit": 100},
            )
            if not ok:
                print(f"FAIL vmanage_list_devices ({device_text})")
                return 1
            print("PASS vmanage_list_devices")
            passed += 1

            payload = json.loads(device_text)
            devices = payload.get("devices", [])
            target = next(
                (
                    device
                    for device in devices
                    if device.get("device_type") == "vedge"
                    and device.get("reachability") == "reachable"
                ),
                devices[0] if devices else None,
            )
            if target is None:
                print("FAIL discovery (no devices)")
                return 1

            system_ip = target.get("system_ip")
            device_uuid = target.get("uuid")
            calls = [
                ("vmanage_get_device_status", {"system_ip": system_ip}),
                ("vmanage_get_device_counters", {"system_ip": system_ip}),
                ("vmanage_get_device_interfaces", {"system_ip": system_ip}),
                ("vmanage_list_tunnels", {"system_ip": system_ip}),
                ("vmanage_get_bfd_sessions", {"system_ip": system_ip}),
                ("vmanage_get_omp_peers", {"system_ip": system_ip}),
                ("vmanage_list_alarms", {}),
                ("vmanage_get_alarm_count", {}),
                ("vmanage_list_events", {"system_ip": system_ip}),
                ("vmanage_list_policies", {}),
                ("vmanage_list_templates", {}),
                ("vmanage_get_running_config", {"device_uuid": device_uuid}),
                ("vmanage_get_system_status", {"system_ip": system_ip}),
                ("vmanage_get_control_status", {"system_ip": system_ip}),
                ("vmanage_get_fabric_summary", {}),
                ("vmanage_assess_fabric_health", {}),
                ("vmanage_diagnose_device", {"system_ip": system_ip}),
                ("vmanage_pre_change_validation", {}),
                ("vmanage_incident_summary", {"audience": "engineer"}),
                ("vmanage_check_version", {}),
            ]

            for name, arguments in calls:
                ok, detail = await _call(
                    session,
                    name,
                    {**arguments, "response_format": "json"},
                )
                if ok:
                    print(f"PASS {name}")
                    passed += 1
                else:
                    print(f"FAIL {name} ({detail})")
                    failed += 1

            print(f"summary passed={passed} failed={failed}")
            return 0 if failed == 0 else 1


def main() -> None:
    raise SystemExit(asyncio.run(audit()))


if __name__ == "__main__":
    main()
