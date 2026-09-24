"""Interactive terminal app for Cisco vManage MCP.

A standalone CLI experience with a beautiful startup sequence
and interactive command shell.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re as _re
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

# Robust .env loading: try multiple locations
_project_root = Path(__file__).resolve().parent.parent.parent
for _env_candidate in [
    _project_root / ".env",          # source tree
    Path.cwd() / ".env",             # current working directory
    Path.home() / ".vmanage-mcp" / ".env",  # home config
]:
    if _env_candidate.is_file():
        load_dotenv(_env_candidate)
        break

# ── ANSI helpers ──────────────────────────────────────────────────────────────

CYAN = "\033[96m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"
CLEAR_LINE = "\033[2K\r"

CISCO_BLUE = "\033[38;5;39m"
CISCO_GREEN = "\033[38;5;48m"


def _c(text: str, color: str) -> str:
    return f"{color}{text}{RESET}"


def _clear_screen() -> None:
    """Clear an ANSI-compatible terminal without invoking a shell."""
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.flush()


# ── ASCII Banner ──────────────────────────────────────────────────────────────

BANNER = f"""{CISCO_BLUE}
    ╔═══════════════════════════════════════════════════════════════╗
    ║                                                               ║
    ║   {BOLD}██████╗██╗███████╗ ██████╗ ██████╗ {RESET}{CISCO_BLUE}                        ║
    ║   {BOLD}██╔════╝██║██╔════╝██╔════╝██╔═══██╗{RESET}{CISCO_BLUE}                       ║
    ║   {BOLD}██║     ██║███████╗██║     ██║   ██║{RESET}{CISCO_BLUE}                       ║
    ║   {BOLD}██║     ██║╚════██║██║     ██║   ██║{RESET}{CISCO_BLUE}                       ║
    ║   {BOLD}╚██████╗██║███████║╚██████╗╚██████╔╝{RESET}{CISCO_BLUE}                       ║
    ║   {BOLD} ╚═════╝╚═╝╚══════╝ ╚═════╝ ╚═════╝ {RESET}{CISCO_BLUE}                      ║
    ║                                                               ║
    ║   {CISCO_GREEN}vManage MCP Console{CISCO_BLUE}         {DIM}SD-WAN Operations Terminal{RESET}{CISCO_BLUE}   ║
    ║                                                               ║
    ╚═══════════════════════════════════════════════════════════════╝{RESET}
"""

# ── Spinner animation ────────────────────────────────────────────────────────

SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


def _spin_print(message: str, duration: float = 0.5) -> None:
    """Show a spinner animation for a fixed duration, then clear."""
    frames = SPINNER_FRAMES
    start = time.monotonic()
    i = 0
    while time.monotonic() - start < duration:
        frame = frames[i % len(frames)]
        sys.stdout.write(f"{CLEAR_LINE}    {CISCO_BLUE}{frame}{RESET} {message}")
        sys.stdout.flush()
        time.sleep(0.08)
        i += 1


def _step_ok(message: str, detail: str = "") -> None:
    detail_str = f"  {DIM}{detail}{RESET}" if detail else ""
    sys.stdout.write(f"{CLEAR_LINE}    {GREEN}✓{RESET} {message}{detail_str}\n")
    sys.stdout.flush()


def _step_fail(message: str, detail: str = "") -> None:
    detail_str = f"  {DIM}{detail}{RESET}" if detail else ""
    sys.stdout.write(f"{CLEAR_LINE}    {RED}✗{RESET} {message}{detail_str}\n")
    sys.stdout.flush()


def _step_warn(message: str, detail: str = "") -> None:
    detail_str = f"  {DIM}{detail}{RESET}" if detail else ""
    sys.stdout.write(f"{CLEAR_LINE}    {YELLOW}!{RESET} {message}{detail_str}\n")
    sys.stdout.flush()


# ── Startup sequence ─────────────────────────────────────────────────────────

def _suppress_noisy_logs():
    """Suppress [AUDIT] and client retry log output from polluting the console UI.

    Must be called after first API call triggers audit module import,
    or we pre-import it here to ensure the logger exists.
    """
    # Force the audit module to load so its logger is initialized
    import cisco_vmanage_mcp.services.audit  # noqa: F401

    # Suppress all cisco_vmanage_mcp loggers that write to stderr/stdout
    for logger_name in ("cisco_vmanage_mcp.audit", "cisco_vmanage_mcp.client", "cisco_vmanage_mcp"):
        _logger = logging.getLogger(logger_name)
        _logger.handlers = [
            h for h in _logger.handlers
            if not isinstance(h, logging.StreamHandler) or h.stream not in (sys.stderr, sys.stdout)
        ]
        if logger_name != "cisco_vmanage_mcp.audit":
            _logger.setLevel(logging.CRITICAL)

    # Also suppress the root logger's stream handlers to catch any leaks
    root = logging.getLogger()
    root.handlers = [
        h for h in root.handlers
        if not isinstance(h, logging.StreamHandler) or h.stream not in (sys.stderr, sys.stdout)
    ]


# ── Shared event loop & client ────────────────────────────────────────────────
# One loop + one authenticated client reused across all commands.
# asyncio.run() must NOT be used — it creates and then closes a new loop each
# time, which destroys the aiohttp session living on the previous loop.

_loop: asyncio.AbstractEventLoop | None = None
_shared_client = None


def _get_loop() -> asyncio.AbstractEventLoop:
    """Return the single persistent event loop (created on first call)."""
    global _loop
    if _loop is None or _loop.is_closed():
        _loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_loop)
    return _loop


def _run(coro):
    """Run a coroutine on the persistent loop (replaces asyncio.run)."""
    return _get_loop().run_until_complete(coro)


def _get_shared_client():
    """Get or create the shared VManageClient instance."""
    global _shared_client
    if _shared_client is None:
        from cisco_vmanage_mcp.client import VManageClient
        _shared_client = VManageClient()
    return _shared_client


async def _ensure_authenticated():
    """Ensure the shared client is authenticated."""
    client = _get_shared_client()
    if client._token is None:
        await client.authenticate()
    return client


def _show_environment_status() -> bool:
    _spin_print("Loading environment...")
    host = os.getenv("VMANAGE_HOST", "")
    username = os.getenv("VMANAGE_USERNAME", "")
    if host:
        detail = f"{username}@{host}" if username else host
        _step_ok("Environment loaded", detail)
        return True
    _step_fail("No VMANAGE_HOST configured")
    print(f"\n    {YELLOW}Set environment variables or create a .env file:{RESET}")
    print(f"    {DIM}VMANAGE_HOST=your-vmanage.example.com{RESET}")
    print(f"    {DIM}VMANAGE_USERNAME=admin{RESET}")
    print(f"    {DIM}VMANAGE_PASSWORD=yourpassword{RESET}\n")
    return False


def _connect_with_retries() -> dict:
    global _shared_client

    info = {"connected": False}
    for attempt in range(1, 4):
        suffix = f" (attempt {attempt}/3)" if attempt > 1 else ""
        _spin_print(f"Testing connectivity{suffix}...")
        info = _run(_test_connection())
        if info["connected"]:
            _step_ok("Connected to vManage", f"{info['response_ms']}ms")
            return info
        if attempt < 3:
            _shared_client = None
            time.sleep(2 * attempt)
    _step_fail("Connection failed", str(info.get("error", "")))
    _step_warn("Continuing in offline mode — commands will retry when you run them")
    return info


def _add_fabric_boot_status(info: dict) -> None:
    if not info.get("connected"):
        return
    _spin_print("Discovering fabric...")
    fabric = _run(_discover_fabric())
    if fabric["success"]:
        _step_ok(
            "Fabric discovered",
            f"{fabric['device_count']} devices across {fabric['site_count']} sites",
        )
        if fabric.get("unreachable", 0) > 0:
            _step_warn(
                f"{fabric['unreachable']} device(s) unreachable",
                "run 'devices' or 'health' for details",
            )
    else:
        _step_warn("Fabric discovery partial", fabric.get("error", ""))
    info.update(fabric)


def _add_alarm_boot_status(info: dict) -> None:
    if not info.get("connected"):
        return
    _spin_print("Checking alarms...")
    alarm_info = _run(_check_alarms())
    if not alarm_info["success"]:
        _step_warn("Alarm check skipped", alarm_info.get("error", ""))
        info.update(alarm_info)
        return
    count = alarm_info["count"]
    critical = alarm_info.get("critical", 0)
    if critical > 0:
        _step_warn(f"{count} active alarms", f"{RED}{critical} critical{RESET}")
    elif count > 0:
        _step_ok(f"{count} active alarms", "none critical")
    else:
        _step_ok("No active alarms")
    info.update(alarm_info)


def _boot_sequence() -> dict:
    """Run the animated startup boot sequence. Returns connection info."""
    from cisco_vmanage_mcp import __version__

    _suppress_noisy_logs()

    print(BANNER)
    print(f"    {DIM}v{__version__}{RESET}")
    print()

    if not _show_environment_status():
        return {"connected": False}

    info = _connect_with_retries()
    _add_fabric_boot_status(info)
    _add_alarm_boot_status(info)

    print()
    _step_ok(f"{BOLD}System ready{RESET}")
    print()

    return info


async def _test_connection() -> dict:
    try:
        start = time.monotonic()
        await _ensure_authenticated()
        ms = round((time.monotonic() - start) * 1000)
        return {"connected": True, "response_ms": ms}
    except Exception as e:
        return {"connected": False, "error": str(e)}


async def _discover_fabric() -> dict:
    try:
        client = await _ensure_authenticated()
        data = await client.get("/dataservice/device")
        devices = data.get("data", [])
        sites = {d.get("site-id") for d in devices if d.get("site-id")}
        unreachable = sum(1 for d in devices if d.get("reachability") != "reachable")
        return {
            "success": True,
            "device_count": len(devices),
            "site_count": len(sites),
            "unreachable": unreachable,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


async def _check_alarms() -> dict:
    try:
        client = await _ensure_authenticated()
        data = await client.get("/dataservice/alarms")
        alarms = data.get("data", [])
        critical = sum(1 for a in alarms if a.get("severity", "").lower() == "critical")
        return {"success": True, "count": len(alarms), "critical": critical}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ── Interactive Shell ─────────────────────────────────────────────────────────

HELP_TEXT = f"""
    {BOLD}Available Commands:{RESET}

    {CYAN}status{RESET}           Test connectivity, show version and device count
    {CYAN}devices{RESET}          List all devices with reachability status
    {CYAN}health{RESET}           Run fabric health assessment with root-cause analysis
    {CYAN}alarms{RESET}           List active alarms by severity
    {CYAN}diagnose <ip>{RESET}    Deep-dive diagnosis on a specific device
    {CYAN}smoke-test{RESET}       Quick connectivity and auth check

    {DIM}help             Show this help message{RESET}
    {DIM}clear            Clear terminal{RESET}
    {DIM}exit / quit      Exit the console{RESET}
"""

PROMPT = f"{CISCO_BLUE}vmanage{RESET}{DIM}:{RESET}{CISCO_GREEN}~{RESET}{BOLD}${RESET} "


# ── Natural language intent matching ────────────────────────────────────────

_INTENT_PATTERNS: list[tuple[str, str]] = [
    # Help / capabilities
    (r"(help|what can|capable|commands?|options?|features?|how (do|does|to)|usage|what .* do)", "help"),
    # Devices
    (r"(device|router|switch|edge|controller|vedge|vbond|vsmart|node|inventor|show me|how many)", "devices"),
    # Health
    (r"(health|fabric|assessment|check.*(fabric|health|network)|how.*(network|fabric|doing)|root.?cause)", "health"),
    # Alarms
    (r"(alarm|alert|warning|critical|incident|issue|problem|wrong|broken|error|fault|notification|any.*(alarm|alert|issue|problem|wrong|broken))", "alarms"),
    # Diagnose (need to extract IP)
    (r"(diagnos|troubleshoot|debug|inspect|investigate|analyze)", "diagnose"),
    # Smoke test
    (r"(smoke|test|verify|validate|quick check|pre.?check|sanity)", "smoke-test"),
    # Status (broad — keep last among specific commands)
    (r"(status|version|connect|connection|up\b|running|info\b|overview|summary)", "status"),
]


def _match_intent(text: str) -> tuple[str | None, list[str]]:
    """Match natural language input to a command. Returns (command, args)."""
    lower = text.lower().strip()

    # Direct command match first
    parts = text.split()
    first = parts[0].lower()
    if first in ("status", "devices", "health", "alarms", "smoke-test", "diagnose", "help",
                 "clear", "exit", "quit", "q"):
        return first, parts[1:]

    # Check if there's an IP address in the input — likely a diagnose request
    ip_match = _re.search(r"\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\b", text)

    # If input contains an IP address, it's almost certainly a diagnose request
    if ip_match:
        return "diagnose", [ip_match.group(1)]

    # Try intent patterns
    for pattern, cmd in _INTENT_PATTERNS:
        if _re.search(pattern, lower):
            # Extract IP address for diagnose
            if cmd == "diagnose":
                if ip_match:
                    return cmd, [ip_match.group(1)]
                return cmd, []
            return cmd, parts[1:]

    return None, []


def _is_exact_command(text: str) -> tuple[str | None, list[str]]:
    """Check if input is an exact CLI command (not natural language)."""
    parts = text.strip().split()
    if not parts:
        return None, []
    first = parts[0].lower()
    # Only match single-word exact commands or 'diagnose <ip>'
    exact_cmds = {"status", "devices", "health", "alarms", "smoke-test", "diagnose", "help"}
    if first in exact_cmds:
        return first, parts[1:]
    return None, []


def _read_shell_input(ai_provider=None) -> tuple[bool, str]:
    try:
        prompt = f"{CISCO_BLUE}you{RESET}{BOLD}:{RESET} " if ai_provider else PROMPT
        return True, input(prompt).strip()
    except (KeyboardInterrupt, EOFError):
        print(f"\n\n    {DIM}Goodbye.{RESET}\n")
        return False, ""


def _handle_universal_command(command: str) -> str:
    if command in ("exit", "quit", "q"):
        print(f"\n    {DIM}Goodbye.{RESET}\n")
        return "exit"
    if command == "clear":
        _clear_screen()
        return "handled"
    if command == "help":
        print(HELP_TEXT)
        return "handled"
    return "unhandled"


def _handle_ai_management(command: str, ai_provider=None) -> tuple[bool, object | None]:
    if command == "ai" and not ai_provider:
        from cisco_vmanage_mcp.ai_providers import login_flow

        provider = login_flow()
        if provider:
            print(f"\n    {DIM}Ask anything about your network.{RESET}\n")
        else:
            print(f"\n    {DIM}No AI connected. Using manual commands.{RESET}\n")
        return True, provider
    if command != "switch":
        return False, ai_provider

    from cisco_vmanage_mcp.ai_providers import (
        _delete_saved_api_key,
        _load_config,
        _save_config,
        login_flow,
    )

    config = _load_config()
    if saved_provider := config.get("provider"):
        _delete_saved_api_key(saved_provider)
    config.pop("provider", None)
    _save_config(config)
    provider = login_flow()
    if provider:
        print()
        return True, provider
    return True, ai_provider


def _run_ai_prompt(ai_provider, raw: str) -> None:
    try:
        print()
        response = ai_provider.chat(raw)
        for line in response.split("\n"):
            print(f"    {line}")
        print()
    except KeyboardInterrupt:
        print(f"\n    {DIM}(cancelled){RESET}\n")
    except Exception as exc:
        print(f"\n    {RED}AI error:{RESET} {exc}\n")


def _run_manual_prompt(raw: str) -> None:
    command, arguments = _match_intent(raw)
    manual_commands = {"status", "devices", "health", "alarms", "smoke-test", "diagnose"}
    if command in manual_commands:
        first_word = raw.split()[0].lower()
        if first_word != command:
            print(f"    {DIM}→ running: {command}{RESET}")
        _run_cli_command(command, arguments)
        return
    print(f"\n    {YELLOW}I don't understand that.{RESET}")
    print(f"    {DIM}Type 'ai' to connect an AI for natural conversation, or use a command:{RESET}")
    print(HELP_TEXT)


def _run_shell(ai_provider=None) -> None:
    """Run the interactive command shell, optionally with AI conversation."""
    if ai_provider:
        print(f"    {DIM}Ask anything about your network in natural language.{RESET}")
        print(f"    {DIM}Type 'switch' to change AI provider, 'help' for commands, 'exit' to quit.{RESET}")
    else:
        print(HELP_TEXT)
        print(f"    {DIM}Tip: Type 'ai' to connect an AI provider for natural conversation.{RESET}")
    print()

    while True:
        keep_running, raw = _read_shell_input(ai_provider)
        if not keep_running:
            break
        if not raw:
            continue

        lower = raw.lower().strip()
        command_state = _handle_universal_command(lower)
        if command_state == "exit":
            break
        if command_state == "handled":
            continue

        handled, ai_provider = _handle_ai_management(lower, ai_provider)
        if handled:
            continue

        exact_cmd, exact_args = _is_exact_command(raw)
        if exact_cmd and exact_cmd != "help":
            _run_cli_command(exact_cmd, exact_args)
            continue

        if ai_provider:
            _run_ai_prompt(ai_provider, raw)
            continue

        _run_manual_prompt(raw)


def _run_cli_command(cmd: str, args: list[str]) -> None:
    """Execute a CLI command and display results with formatting."""
    print()
    try:
        if cmd == "status":
            _cmd_status()
        elif cmd == "devices":
            _cmd_devices()
        elif cmd == "health":
            _cmd_health()
        elif cmd == "alarms":
            _cmd_alarms()
        elif cmd == "diagnose":
            if not args:
                print(f"    {YELLOW}Usage:{RESET} diagnose <system-ip>")
                print(f"    {DIM}Example: diagnose 10.10.1.11{RESET}")
            else:
                _cmd_diagnose(args[0])
        elif cmd == "smoke-test":
            _cmd_smoke_test()
    except Exception as e:
        err_msg = str(e).lower()
        if "503" in err_msg or "auth" in err_msg or "xsrf" in err_msg or "token" in err_msg:
            # Session expired — reset client so next command gets a fresh one
            global _shared_client
            _shared_client = None
            print(f"    {RED}Error:{RESET} {e}")
            print(f"    {DIM}Session expired — try the command again.{RESET}")
        else:
            print(f"    {RED}Error:{RESET} {e}")
    print()


def _cmd_status():
    async def _do():
        client = await _ensure_authenticated()
        start = time.monotonic()
        data = await client.get("/dataservice/device")
        ms = round((time.monotonic() - start) * 1000)
        devices = data.get("data", [])
        controllers = sum(1 for d in devices if d.get("device-type") != "vedge")
        edges = sum(1 for d in devices if d.get("device-type") == "vedge")
        return {
            "host": client.host,
            "port": client.port,
            "devices": len(devices),
            "controllers": controllers,
            "edges": edges,
            "ms": ms,
        }

    r = _run(_do())
    print(f"    {BOLD}vManage Connection{RESET}  {GREEN}OK{RESET}")
    print(f"    Host:        {r['host']}:{r['port']}")
    print(f"    Devices:     {r['devices']} total ({r['controllers']} controllers, {r['edges']} edges)")
    print(f"    Response:    {r['ms']}ms")


def _cmd_devices():
    async def _do():
        client = await _ensure_authenticated()
        data = await client.get("/dataservice/device")
        return data.get("data", [])

    devices = _run(_do())
    if not devices:
        print(f"    {DIM}No devices found.{RESET}")
        return

    # Header
    print(f"    {BOLD}{'Hostname':<20} {'System IP':<16} {'Type':<10} {'Model':<18} {'Status':<14} {'Site'}{RESET}")
    print(f"    {'─' * 85}")

    for d in devices:
        hostname = d.get("host-name", "N/A")[:19]
        system_ip = d.get("system-ip", "N/A")
        dtype = d.get("device-type", "N/A")
        model = d.get("device-model", "N/A")[:17]
        reachable = d.get("reachability", "N/A")
        site = d.get("site-id", "N/A")

        if reachable == "reachable":
            status_str = f"{GREEN}● reachable{RESET}   "
        else:
            status_str = f"{RED}● unreachable{RESET} "

        print(f"    {hostname:<20} {system_ip:<16} {dtype:<10} {model:<18} {status_str} {site}")

    print(f"\n    {DIM}{len(devices)} device(s) total{RESET}")


def _cmd_health():
    from cisco_vmanage_mcp.services.correlation import correlate_fabric_state

    _spin_print("Analyzing fabric health...")

    async def _do():
        client = await _ensure_authenticated()
        return await correlate_fabric_state(client)

    report = _run(_do())
    fabric = report.fabric_report
    health = fabric.overall_health.value.upper()

    health_color = GREEN if health == "HEALTHY" else YELLOW if health == "DEGRADED" else RED
    sys.stdout.write(CLEAR_LINE)
    print(f"    {BOLD}Fabric Health:{RESET}  {health_color}{health}{RESET}")
    print()

    controllers = [d for d in fabric.devices if d.device_type != "vedge"]
    edges = [d for d in fabric.devices if d.device_type == "vedge"]
    print(f"    Controllers: {len(controllers)} ({sum(1 for c in controllers if c.reachable)} reachable)")
    print(f"    WAN Edges:   {len(edges)} ({sum(1 for e in edges if e.reachable)} reachable)")

    if fabric.alarm_counts:
        parts = [f"{v} {k}" for k, v in sorted(fabric.alarm_counts.items())]
        print(f"    Alarms:      {', '.join(parts)}")

    if report.root_causes:
        print(f"\n    {BOLD}Root-Cause Hypotheses:{RESET}")
        for rc in report.root_causes:
            conf_color = RED if rc.confidence == "high" else YELLOW if rc.confidence == "medium" else DIM
            print(f"    {rc.rank}. {rc.hypothesis}  {conf_color}[{rc.confidence}]{RESET}")
            for ev in rc.supporting_evidence:
                print(f"       {DIM}→ {ev}{RESET}")
            if rc.suggested_checks:
                print(f"       {CYAN}Checks: {'; '.join(rc.suggested_checks)}{RESET}")

    if report.impact and report.impact.scope != "none":
        print(f"\n    {BOLD}Impact:{RESET} {report.impact.scope}")
        if hasattr(report.impact, "estimated_user_impact"):
            print(f"    {report.impact.estimated_user_impact}")

    print(f"\n    {DIM}Data Sources:{RESET}")
    for fr in fabric.fetch_results:
        icon = f"{GREEN}✓{RESET}" if fr.success else f"{RED}✗{RESET}"
        print(f"    {icon} {fr.source.value} ({fr.duration_ms:.0f}ms)")


def _cmd_alarms():
    async def _do():
        client = await _ensure_authenticated()
        data = await client.get("/dataservice/alarms")
        return data.get("data", [])

    alarms = _run(_do())
    if not alarms:
        print(f"    {GREEN}No active alarms.{RESET}")
        return

    severity_order = {"critical": 0, "major": 1, "medium": 2, "minor": 3}
    alarms.sort(key=lambda a: (
        severity_order.get(a.get("severity", "").lower(), 9),
        -int(a.get("entry_time", 0) or 0),
    ))

    print(f"    {BOLD}{'Severity':<12} {'Type':<32} {'Device':<20} {'Message'}{RESET}")
    print(f"    {'─' * 85}")

    for a in alarms[:25]:
        sev = a.get("severity", "N/A")
        sev_color = {
            "Critical": RED,
            "Major": YELLOW,
            "Medium": CYAN,
            "Minor": DIM,
        }.get(sev, "")
        atype = a.get("type", a.get("rule_name_display", "N/A"))[:31]
        device = a.get("host_name", a.get("system_ip", "N/A"))[:19]
        msg = (a.get("message", "") or "")[:40]
        print(f"    {sev_color}{'●':<2} {sev:<10}{RESET} {atype:<32} {device:<20} {DIM}{msg}{RESET}")

    print(f"\n    {DIM}{len(alarms)} alarm(s) total{RESET}")


def _cmd_diagnose(system_ip: str):
    from cisco_vmanage_mcp.services.correlation import diagnose_device

    _spin_print(f"Diagnosing {system_ip}...")

    async def _do():
        client = await _ensure_authenticated()
        return await diagnose_device(client, system_ip)

    report, device = _run(_do())
    sys.stdout.write(CLEAR_LINE)

    if device:
        health = device.overall_health.value.upper()
        health_color = GREEN if health == "HEALTHY" else YELLOW if health == "DEGRADED" else RED

        print(f"    {BOLD}Device Diagnosis:{RESET} {device.hostname} ({device.system_ip})")
        print(f"    {'─' * 50}")
        print(f"    Site:          {device.site_id}")
        reachable_str = f"{GREEN}Yes{RESET}" if device.reachable else f"{RED}No{RESET}"
        print(f"    Reachable:     {reachable_str}")
        print(f"    Health:        {health_color}{health}{RESET}")
        print(f"    BFD Sessions:  {device.bfd_sessions}")
        print(f"    Control Conns: {device.control_connections}")
    else:
        print(f"    {YELLOW}No device data found.{RESET}")

    if report.impact:
        print(f"    Failure Scope: {CYAN}{report.impact.scope}{RESET}")

    if report.root_causes:
        print(f"\n    {BOLD}Root-Cause Hypotheses:{RESET}")
        for rc in report.root_causes:
            conf_color = RED if rc.confidence == "high" else YELLOW if rc.confidence == "medium" else DIM
            print(f"    {rc.rank}. {rc.hypothesis}  {conf_color}[{rc.confidence}]{RESET}")

    if report.narrative:
        print(f"\n    {DIM}{report.narrative}{RESET}")


def _cmd_smoke_test():
    checks = []

    async def _do():
        try:
            start = time.monotonic()
            client = await _ensure_authenticated()
            ms = round((time.monotonic() - start) * 1000)
            checks.append(("authentication", True, f"{ms}ms", ""))

            start = time.monotonic()
            data = await client.get("/dataservice/device")
            ms = round((time.monotonic() - start) * 1000)
            count = len(data.get("data", []))
            checks.append(("device_api", True, f"{ms}ms", f"{count} devices"))

            start = time.monotonic()
            await client.get("/dataservice/alarms/count")
            ms = round((time.monotonic() - start) * 1000)
            checks.append(("alarm_api", True, f"{ms}ms", ""))
        except Exception as e:
            checks.append(("connection", False, "", str(e)))

    _run(_do())

    all_pass = all(c[1] for c in checks)
    header = f"{GREEN}PASS{RESET}" if all_pass else f"{RED}FAIL{RESET}"
    print(f"    {BOLD}Smoke Test:{RESET}  {header}")
    print()
    for name, ok, duration, extra in checks:
        icon = f"{GREEN}✓{RESET}" if ok else f"{RED}✗{RESET}"
        parts = [f"    {icon} {name}"]
        if duration:
            parts.append(f"{DIM}({duration}){RESET}")
        if extra:
            parts.append(f"{DIM}[{extra}]{RESET}")
        print(" ".join(parts))


# ── Entry point ──────────────────────────────────────────────────────────────

def main():
    """Launch the interactive vManage console."""
    # Clear screen for fresh start
    _clear_screen()

    info = _boot_sequence()

    if not info.get("connected"):
        print(f"    {DIM}vManage is temporarily unavailable — commands will retry automatically.{RESET}")
        print(f"    {DIM}You can still connect an AI provider and use the console.{RESET}")
        print()

    # Wire shared runtime into AI providers so tool calls reuse our loop + client
    from cisco_vmanage_mcp.ai_providers import auto_connect, login_flow, set_app_runtime
    set_app_runtime(_run, _ensure_authenticated)

    # Auto-connect using saved credentials or env var API keys (no prompts)
    ai_provider = auto_connect()

    # If no auto-credentials, fall back to interactive login
    if not ai_provider:
        ai_provider = login_flow()

    if not ai_provider:
        print(f"\n    {DIM}No AI provider selected — using manual commands.{RESET}")
        print(f"    {DIM}Type 'ai' anytime to connect one.{RESET}")

    try:
        _run_shell(ai_provider=ai_provider)
    finally:
        # Clean up shared client on exit
        if _shared_client is not None:
            try:
                _get_loop().run_until_complete(_shared_client.close())
            except Exception as exc:
                logging.getLogger("cisco_vmanage_mcp.app").debug(
                    "Failed to close shared client: %s", exc
                )


if __name__ == "__main__":
    main()
