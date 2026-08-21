"""AI provider integration for the vManage Console.

Supports Claude (Anthropic), GPT (OpenAI), and Gemini (Google) with
tool-calling so the AI can query the SD-WAN fabric conversationally.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

# ── SSL / optional custom CA bundle ───────────────────────────────────────────

def _ca_bundle_path() -> str | None:
    """Return the path to an optional custom CA bundle, if present."""
    bundle = Path(__file__).resolve().parent.parent.parent / "certs" / "ca-bundle.pem"
    if bundle.exists():
        return str(bundle)
    return None


# ── Shared runtime (set by app.py before tool execution) ─────────────────────
# These allow AI tool calls to reuse the app's persistent event loop and client.
_app_run = None       # callable: runs a coroutine on the persistent loop
_app_client = None    # callable: returns the shared, authenticated VManageClient


def set_app_runtime(run_fn, client_fn):
    """Called by app.py to inject the persistent loop runner and client getter."""
    global _app_run, _app_client
    _app_run = run_fn
    _app_client = client_fn

# ── Config persistence ────────────────────────────────────────────────────────

CONFIG_DIR = Path.home() / ".vmanage-mcp"
CONFIG_FILE = CONFIG_DIR / "config.json"


def _load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text())
        except Exception:
            return {}
    return {}


def _save_config(config: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(config, indent=2))
    # Restrict permissions to owner only
    CONFIG_FILE.chmod(0o600)


# ── ANSI ──────────────────────────────────────────────────────────────────────

CYAN = "\033[96m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"
CISCO_BLUE = "\033[38;5;39m"
CISCO_GREEN = "\033[38;5;48m"

# ── Provider definitions ──────────────────────────────────────────────────────

PROVIDERS = {
    "claude": {
        "name": "Claude (Anthropic)",
        "env_key": "ANTHROPIC_API_KEY",
        "key_url": "https://console.anthropic.com/settings/keys",
        "icon": "🟠",
    },
    "openai": {
        "name": "GPT (OpenAI)",
        "env_key": "OPENAI_API_KEY",
        "key_url": "https://platform.openai.com/api-keys",
        "icon": "🟢",
    },
    "gemini": {
        "name": "Gemini (Google)",
        "env_key": "GOOGLE_API_KEY",
        "key_url": "https://aistudio.google.com/apikey",
        "icon": "🔵",
    },
}

# ── Tool definitions (shared across all providers) ───────────────────────────

SYSTEM_PROMPT = """You are an expert Cisco SD-WAN network operations assistant embedded in the vManage MCP Console.
You have access to a live vManage instance and can query it using the tools provided.

Guidelines:
- When the user asks about the network, devices, health, or alarms, USE the tools to get real data.
- Present data in a clean, readable format for the terminal. No markdown headers or formatting — use plain text.
- Be concise and actionable. If something is wrong, explain what and suggest what to check.
- For follow-up questions about data you already retrieved, you can reference previous results.
- If the user asks something unrelated to networking, you can still help but prioritize network topics.
- Never fabricate device data or alarm information — always fetch from the tools.
"""

TOOL_DEFINITIONS = [
    {
        "name": "get_device_list",
        "description": "Get all SD-WAN devices with hostname, system IP, model, device type, reachability status, and site ID.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_alarms",
        "description": "Get all active alarms with severity, type, device hostname, system IP, and message.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_fabric_health",
        "description": "Run a comprehensive fabric health assessment with root-cause analysis, impact scope, and suggested remediation.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "diagnose_device",
        "description": "Run deep-dive diagnosis on a specific device by system IP. Includes BFD sessions, control connections, failure scope, and root causes.",
        "parameters": {
            "type": "object",
            "properties": {
                "system_ip": {
                    "type": "string",
                    "description": "The system IP address of the device to diagnose (e.g. 10.10.1.11)",
                }
            },
            "required": ["system_ip"],
        },
    },
    {
        "name": "get_status",
        "description": "Check vManage connectivity, response time, and device count summary.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "run_smoke_test",
        "description": "Run quick connectivity and authentication check against vManage. Returns pass/fail for auth, device API, and alarm API.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
]


# ── Tool execution (calls real vManage) ───────────────────────────────────────

def _exec_tool(name: str, args: dict) -> str:
    """Execute a vManage tool and return the result as a string."""
    try:
        if name == "get_device_list":
            return _tool_devices()
        elif name == "get_alarms":
            return _tool_alarms()
        elif name == "get_fabric_health":
            return _tool_health()
        elif name == "diagnose_device":
            return _tool_diagnose(args.get("system_ip", ""))
        elif name == "get_status":
            return _tool_status()
        elif name == "run_smoke_test":
            return _tool_smoke()
        else:
            return f"Unknown tool: {name}"
    except Exception as e:
        return f"Tool error: {e}"


def _tool_devices() -> str:
    async def _do():
        client = await _app_client()
        data = await client.get("/dataservice/device")
        return data.get("data", [])

    devices = _app_run(_do())
    if not devices:
        return "No devices found."

    lines = []
    for d in devices:
        lines.append(json.dumps({
            "hostname": d.get("host-name", "N/A"),
            "system_ip": d.get("system-ip", "N/A"),
            "type": d.get("device-type", "N/A"),
            "model": d.get("device-model", "N/A"),
            "reachability": d.get("reachability", "N/A"),
            "site_id": d.get("site-id", "N/A"),
        }))
    return f"[{','.join(lines)}]"


def _tool_alarms() -> str:
    async def _do():
        client = await _app_client()
        data = await client.get("/dataservice/alarms")
        return data.get("data", [])

    alarms = _app_run(_do())
    if not alarms:
        return "No active alarms."

    results = []
    for a in alarms:
        results.append({
            "severity": a.get("severity", "N/A"),
            "type": a.get("type", a.get("rule_name_display", "N/A")),
            "device": a.get("host_name", a.get("system_ip", "N/A")),
            "message": (a.get("message", "") or "")[:120],
        })
    return json.dumps(results)


def _tool_health() -> str:
    from cisco_vmanage_mcp.services.correlation import correlate_fabric_state

    async def _do():
        client = await _app_client()
        return await correlate_fabric_state(client)

    report = _app_run(_do())
    fabric = report.fabric_report

    result = {
        "overall_health": fabric.overall_health.value,
        "device_count": len(fabric.devices),
        "controllers": len([d for d in fabric.devices if d.device_type != "vedge"]),
        "edges": len([d for d in fabric.devices if d.device_type == "vedge"]),
        "unreachable": len([d for d in fabric.devices if not d.reachable]),
        "alarm_counts": fabric.alarm_counts,
    }

    if report.root_causes:
        result["root_causes"] = [
            {
                "rank": rc.rank,
                "hypothesis": rc.hypothesis,
                "confidence": rc.confidence,
                "evidence": rc.supporting_evidence,
                "suggested_checks": rc.suggested_checks,
            }
            for rc in report.root_causes
        ]

    if report.impact:
        result["impact"] = {
            "scope": report.impact.scope,
            "estimated_user_impact": getattr(report.impact, "estimated_user_impact", ""),
        }

    return json.dumps(result)


def _tool_diagnose(system_ip: str) -> str:
    from cisco_vmanage_mcp.services.correlation import diagnose_device

    if not system_ip:
        return "Error: system_ip is required."

    async def _do():
        client = await _app_client()
        return await diagnose_device(client, system_ip)

    report = _app_run(_do())
    result: dict[str, Any] = {}

    if hasattr(report, "device") and report.device:
        d = report.device
        result["device"] = {
            "hostname": d.hostname,
            "system_ip": d.system_ip,
            "site_id": d.site_id,
            "reachable": d.reachable,
            "health": d.overall_health.value,
            "bfd_sessions": d.bfd_sessions,
            "control_connections": d.control_connections,
        }

    if hasattr(report, "is_isolated"):
        result["failure_scope"] = "isolated" if report.is_isolated else "widespread"

    if hasattr(report, "root_causes") and report.root_causes:
        result["root_causes"] = [
            {"rank": rc.rank, "hypothesis": rc.hypothesis, "confidence": rc.confidence}
            for rc in report.root_causes
        ]

    if hasattr(report, "narrative") and report.narrative:
        result["narrative"] = report.narrative

    return json.dumps(result) if result else "No diagnostic data available."


def _tool_status() -> str:
    async def _do():
        client = await _app_client()
        start = time.monotonic()
        data = await client.get("/dataservice/device")
        ms = round((time.monotonic() - start) * 1000)
        devices = data.get("data", [])
        return {
            "host": client.host,
            "port": client.port,
            "connected": True,
            "response_ms": ms,
            "total_devices": len(devices),
            "controllers": sum(1 for d in devices if d.get("device-type") != "vedge"),
            "edges": sum(1 for d in devices if d.get("device-type") == "vedge"),
        }

    return json.dumps(_app_run(_do()))


def _tool_smoke() -> str:
    checks: list[dict] = []

    async def _do():
        try:
            client = await _app_client()
            start = time.monotonic()
            data = await client.get("/dataservice/device")
            ms = round((time.monotonic() - start) * 1000)
            checks.append({"check": "device_api", "pass": True, "ms": ms, "devices": len(data.get("data", []))})

            start = time.monotonic()
            await client.get("/dataservice/alarms/count")
            ms = round((time.monotonic() - start) * 1000)
            checks.append({"check": "alarm_api", "pass": True, "ms": ms})
        except Exception as e:
            checks.append({"check": "connection", "pass": False, "error": str(e)})

    _app_run(_do())
    return json.dumps({"overall": all(c["pass"] for c in checks), "checks": checks})


# ── Provider base ─────────────────────────────────────────────────────────────

class AIProvider:
    """Base class for AI providers with tool-calling support."""

    name: str = "base"
    display_name: str = "Base"

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.conversation: list[dict] = []

    def chat(self, user_message: str) -> str:
        """Send a message and return the AI's response, executing tools as needed."""
        raise NotImplementedError


# ── Claude (Anthropic) ────────────────────────────────────────────────────────

class ClaudeProvider(AIProvider):
    name = "claude"
    display_name = "Claude (Anthropic)"

    def __init__(self, api_key: str):
        super().__init__(api_key)
        import anthropic
        import httpx
        ca = _ca_bundle_path()
        http_client = httpx.Client(verify=ca) if ca else None
        self.client = anthropic.Anthropic(api_key=api_key, http_client=http_client)
        self.model = "claude-sonnet-4-20250514"
        self._tools = self._convert_tools()

    def _convert_tools(self) -> list[dict]:
        """Convert tool defs to Anthropic format."""
        tools = []
        for t in TOOL_DEFINITIONS:
            tools.append({
                "name": t["name"],
                "description": t["description"],
                "input_schema": t["parameters"],
            })
        return tools

    def chat(self, user_message: str) -> str:
        self.conversation.append({"role": "user", "content": user_message})

        while True:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                tools=self._tools,
                messages=self.conversation,
            )

            # Collect response
            self.conversation.append({"role": "assistant", "content": response.content})

            # Check if we need to execute tools
            if response.stop_reason == "tool_use":
                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        sys.stdout.write(f"    {DIM}⚡ querying: {block.name}{RESET}")
                        sys.stdout.flush()
                        result = _exec_tool(block.name, block.input)
                        sys.stdout.write(f"\r\033[2K")
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result,
                        })
                self.conversation.append({"role": "user", "content": tool_results})
                continue  # Let the AI process the tool results

            # Extract text response
            text_parts = [b.text for b in response.content if hasattr(b, "text")]
            return "\n".join(text_parts) if text_parts else "(No response)"


# ── GPT (OpenAI) ──────────────────────────────────────────────────────────────

class OpenAIProvider(AIProvider):
    name = "openai"
    display_name = "GPT (OpenAI)"

    def __init__(self, api_key: str):
        super().__init__(api_key)
        from openai import OpenAI
        import httpx
        ca = _ca_bundle_path()
        http_client = httpx.Client(verify=ca) if ca else None
        self.client = OpenAI(api_key=api_key, http_client=http_client)
        self.model = "gpt-4o"
        self._tools = self._convert_tools()
        self.messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]

    def _convert_tools(self) -> list[dict]:
        tools = []
        for t in TOOL_DEFINITIONS:
            tools.append({
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["parameters"],
                },
            })
        return tools

    def chat(self, user_message: str) -> str:
        self.messages.append({"role": "user", "content": user_message})

        while True:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=self.messages,
                tools=self._tools,
                tool_choice="auto",
            )

            msg = response.choices[0].message
            self.messages.append(msg)

            if msg.tool_calls:
                for tc in msg.tool_calls:
                    args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                    sys.stdout.write(f"    {DIM}⚡ querying: {tc.function.name}{RESET}")
                    sys.stdout.flush()
                    result = _exec_tool(tc.function.name, args)
                    sys.stdout.write(f"\r\033[2K")
                    self.messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result,
                    })
                continue

            return msg.content or "(No response)"


# ── Gemini (Google) ───────────────────────────────────────────────────────────

class GeminiProvider(AIProvider):
    name = "gemini"
    display_name = "Gemini (Google)"

    def __init__(self, api_key: str):
        super().__init__(api_key)
        import os
        ca = _ca_bundle_path()
        if ca:
            os.environ.setdefault("SSL_CERT_FILE", ca)
            os.environ.setdefault("REQUESTS_CA_BUNDLE", ca)
        from google import genai
        self.genai_client = genai.Client(api_key=api_key)
        self.model = "gemini-2.5-flash"
        self._tools = self._convert_tools()
        self._chat = None

    def _convert_tools(self) -> list[dict]:
        from google.genai import types
        declarations = []
        for t in TOOL_DEFINITIONS:
            props = t["parameters"].get("properties", {})
            schema_props = {}
            for pname, pdef in props.items():
                schema_props[pname] = types.Schema(
                    type=types.Type.STRING,
                    description=pdef.get("description", ""),
                )
            declarations.append(types.FunctionDeclaration(
                name=t["name"],
                description=t["description"],
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties=schema_props if schema_props else None,
                    required=t["parameters"].get("required", []) or None,
                ) if props else None,
            ))
        return [types.Tool(function_declarations=declarations)]

    def chat(self, user_message: str) -> str:
        from google.genai import types

        if self._chat is None:
            self._chat = self.genai_client.chats.create(
                model=self.model,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    tools=self._tools,
                ),
            )

        response = self._chat.send_message(user_message)

        # Handle tool calls in a loop
        max_rounds = 10
        for _ in range(max_rounds):
            # Check for function calls
            fn_calls = []
            for part in response.candidates[0].content.parts:
                if part.function_call:
                    fn_calls.append(part.function_call)

            if not fn_calls:
                break

            # Execute tool calls
            tool_responses = []
            for fc in fn_calls:
                args = dict(fc.args) if fc.args else {}
                sys.stdout.write(f"    {DIM}⚡ querying: {fc.name}{RESET}")
                sys.stdout.flush()
                result = _exec_tool(fc.name, args)
                sys.stdout.write(f"\r\033[2K")
                tool_responses.append(types.Part.from_function_response(
                    name=fc.name,
                    response={"result": result},
                ))

            response = self._chat.send_message(tool_responses)

        # Extract text
        text_parts = []
        for part in response.candidates[0].content.parts:
            if part.text:
                text_parts.append(part.text)
        return "\n".join(text_parts) if text_parts else "(No response)"


# ── Provider factory ──────────────────────────────────────────────────────────

PROVIDER_CLASSES = {
    "claude": ClaudeProvider,
    "openai": OpenAIProvider,
    "gemini": GeminiProvider,
}


def create_provider(provider_name: str, api_key: str) -> AIProvider:
    """Create an AI provider instance."""
    cls = PROVIDER_CLASSES.get(provider_name)
    if not cls:
        raise ValueError(f"Unknown provider: {provider_name}")
    return cls(api_key)


def validate_api_key(provider_name: str, api_key: str) -> tuple[bool, str]:
    """Validate that an API key works by making a minimal request."""
    try:
        provider = create_provider(provider_name, api_key)
        # Make a minimal request to validate
        result = provider.chat("Reply with just the word OK.")
        if result and len(result) < 200:
            return True, "API key validated"
        return True, "Connected"
    except Exception as e:
        err = str(e)
        if "auth" in err.lower() or "api key" in err.lower() or "invalid" in err.lower():
            return False, "Invalid API key"
        elif "module" in err.lower() or "import" in err.lower():
            return False, f"Missing library: pip install {_get_pip_package(provider_name)}"
        return False, f"Connection error: {err[:100]}"


def _get_pip_package(provider_name: str) -> str:
    return {"claude": "anthropic", "openai": "openai", "gemini": "google-genai"}.get(provider_name, "")


# ── Login flow ────────────────────────────────────────────────────────────────

def _check_library(provider_name: str) -> bool:
    """Check if the required library is installed."""
    try:
        if provider_name == "claude":
            import anthropic  # noqa: F401
        elif provider_name == "openai":
            import openai  # noqa: F401
        elif provider_name == "gemini":
            from google import genai  # noqa: F401
        return True
    except ImportError:
        return False


def auto_connect() -> AIProvider | None:
    """Silently reconnect using saved config, or prompt if multiple env keys found.

    Returns an AIProvider instance or None if no saved/auto credentials found.
    """
    # 1. Try saved config
    config = _load_config()
    saved_provider = config.get("provider")
    saved_key = config.get("api_key")

    if saved_provider and saved_key and _check_library(saved_provider):
        try:
            provider = create_provider(saved_provider, saved_key)
            info = PROVIDERS.get(saved_provider, {})
            print(f"    {GREEN}✓{RESET} Connected to {info.get('icon', '')} {info.get('name', saved_provider)}")
            print(f"    {DIM}Type 'switch' to change provider{RESET}")
            return provider
        except Exception:
            pass  # Fall through to env var check

    # 2. Collect all available providers from env vars
    available: list[tuple[str, dict, str]] = []  # (key, info, api_key)
    for key, info in PROVIDERS.items():
        env_key = info.get("env_key", "")
        api_key = os.getenv(env_key, "")
        # Skip placeholder values
        if api_key and not api_key.startswith("YOUR_") and _check_library(key):
            available.append((key, info, api_key))

    if not available:
        return None

    # 3. If only one provider available, auto-connect silently
    if len(available) == 1:
        key, info, api_key = available[0]
        try:
            provider = create_provider(key, api_key)
            print(f"    {GREEN}✓{RESET} Connected to {info['icon']} {info['name']} {DIM}(from {info['env_key']}){RESET}")
            config["provider"] = key
            config["api_key"] = api_key
            _save_config(config)
            return provider
        except Exception:
            return None

    # 4. Multiple providers available — let user choose
    print(f"\n    {BOLD}Multiple AI providers detected:{RESET}\n")
    for i, (key, info, api_key) in enumerate(available, 1):
        masked = api_key[:8] + "..." + api_key[-4:]
        print(f"    {BOLD}{i}{RESET}  {info['icon']} {info['name']}  {DIM}{masked}{RESET}")
    print(f"\n    {DIM}0  Skip — use manual commands only{RESET}\n")

    try:
        choice = input(f"    Select provider {DIM}[1-{len(available)}]{RESET}: ").strip()
    except (KeyboardInterrupt, EOFError):
        return None

    if choice == "0" or not choice:
        return None

    try:
        idx = int(choice) - 1
        if idx < 0 or idx >= len(available):
            print(f"    {RED}Invalid choice.{RESET}")
            return None
    except ValueError:
        print(f"    {RED}Invalid choice.{RESET}")
        return None

    key, info, api_key = available[idx]
    try:
        provider = create_provider(key, api_key)
        print(f"    {GREEN}✓{RESET} Connected to {info['icon']} {info['name']}")
        config["provider"] = key
        config["api_key"] = api_key
        _save_config(config)
        print(f"    {DIM}Type 'switch' to change provider{RESET}")
        return provider
    except Exception as e:
        print(f"    {RED}✗{RESET} Failed: {e}")
        return None


def login_flow() -> AIProvider | None:
    """Interactive login: select provider, enter API key, validate, save config.

    Returns an AIProvider instance or None if the user cancels.
    """
    config = _load_config()

    # Check if we have a saved session
    saved_provider = config.get("provider")
    saved_key = config.get("api_key")

    if saved_provider and saved_key:
        info = PROVIDERS.get(saved_provider, {})
        print(f"\n    {BOLD}Saved login found:{RESET} {info.get('icon', '')} {info.get('name', saved_provider)}")
        print()

        try:
            choice = input(f"    Use saved login? {DIM}[Y/n/switch]{RESET} ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            return None

        if choice in ("", "y", "yes"):
            if not _check_library(saved_provider):
                pkg = _get_pip_package(saved_provider)
                print(f"\n    {RED}Missing library.{RESET} Run: {CYAN}pip install {pkg}{RESET}")
                return None
            try:
                provider = create_provider(saved_provider, saved_key)
                print(f"    {GREEN}✓{RESET} Logged in as {info.get('icon', '')} {info.get('name', saved_provider)}")
                return provider
            except Exception as e:
                print(f"    {RED}✗{RESET} Login failed: {e}")
                print(f"    {DIM}Clearing saved credentials...{RESET}")
                config.pop("provider", None)
                config.pop("api_key", None)
                _save_config(config)
        elif choice in ("n", "no"):
            return None
        # Fall through to provider selection for "switch"

    # Provider selection
    print(f"\n    {BOLD}Select AI Provider:{RESET}")
    print()

    provider_list = list(PROVIDERS.items())
    for i, (key, info) in enumerate(provider_list, 1):
        installed = _check_library(key)
        status = f"{GREEN}installed{RESET}" if installed else f"{DIM}not installed{RESET}"
        print(f"    {BOLD}{i}{RESET}  {info['icon']} {info['name']}  {DIM}({status}){RESET}")

    print(f"\n    {DIM}0  Skip — use manual commands instead{RESET}")
    print()

    try:
        choice = input(f"    Select provider {DIM}[1-{len(provider_list)}]{RESET}: ").strip()
    except (KeyboardInterrupt, EOFError):
        return None

    if choice == "0" or not choice:
        return None

    try:
        idx = int(choice) - 1
        if idx < 0 or idx >= len(provider_list):
            print(f"    {RED}Invalid choice.{RESET}")
            return None
    except ValueError:
        print(f"    {RED}Invalid choice.{RESET}")
        return None

    provider_key, info = provider_list[idx]

    # Check library
    if not _check_library(provider_key):
        pkg = _get_pip_package(provider_key)
        print(f"\n    {YELLOW}Library not installed.{RESET} Install it with:")
        print(f"    {CYAN}pip install {pkg}{RESET}")
        print()
        try:
            do_install = input(f"    Install now? {DIM}[Y/n]{RESET} ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            return None

        if do_install in ("", "y", "yes"):
            print(f"    {DIM}Installing {pkg}...{RESET}")
            import subprocess
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", pkg],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                print(f"    {RED}Install failed:{RESET} {result.stderr[:200]}")
                return None
            print(f"    {GREEN}✓{RESET} Installed {pkg}")
        else:
            return None

    # Get API key
    env_key = info.get("env_key", "")
    existing_key = os.getenv(env_key, "")

    if existing_key:
        masked = existing_key[:8] + "..." + existing_key[-4:]
        print(f"\n    {DIM}Found {env_key} in environment: {masked}{RESET}")
        try:
            use_env = input(f"    Use this key? {DIM}[Y/n]{RESET} ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            return None
        if use_env in ("", "y", "yes"):
            api_key = existing_key
        else:
            api_key = None
    else:
        api_key = None

    if not api_key:
        print(f"\n    Get your API key from: {CYAN}{info['key_url']}{RESET}")
        print()
        try:
            api_key = input(f"    Enter {info['name']} API key: ").strip()
        except (KeyboardInterrupt, EOFError):
            return None

        if not api_key:
            print(f"    {RED}No key entered.{RESET}")
            return None

    # Validate
    print(f"\n    {DIM}Validating API key...{RESET}", end="", flush=True)
    ok, msg = validate_api_key(provider_key, api_key)
    if ok:
        print(f"\r\033[2K    {GREEN}✓{RESET} {msg}")
    else:
        print(f"\r\033[2K    {RED}✗{RESET} {msg}")
        return None

    # Save
    try:
        save = input(f"    Save login for next time? {DIM}[Y/n]{RESET} ").strip().lower()
    except (KeyboardInterrupt, EOFError):
        save = "n"

    if save in ("", "y", "yes"):
        config["provider"] = provider_key
        config["api_key"] = api_key
        _save_config(config)
        print(f"    {DIM}Saved to {CONFIG_FILE}{RESET}")

    # Create provider
    provider = create_provider(provider_key, api_key)
    print(f"\n    {GREEN}✓{RESET} Ready — {info['icon']} {info['name']}")
    return provider