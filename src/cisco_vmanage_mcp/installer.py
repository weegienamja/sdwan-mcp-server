"""CLI installer for configuring MCP clients to use cisco-vmanage-mcp.

Detects installed MCP clients (Claude Desktop, Claude Code, Cursor),
prompts for vManage credentials, and writes the correct MCP server
configuration into each selected client's config JSON.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import platform
import shutil

# Required only for fixed-argument Claude CLI registration.
import subprocess  # nosec B404
import sys
from pathlib import Path
from typing import Any

CONFIG_DIR = Path.home() / ".vmanage-mcp"
CREDENTIALS_FILE = CONFIG_DIR / "vmanage.env"


# --- MCP client config paths ---

def _get_client_configs() -> dict[str, Path]:
    """Return config file paths for known MCP clients on this OS."""
    system = platform.system()
    configs: dict[str, Path] = {}

    if system == "Darwin":
        home = Path.home()
        configs["Claude Desktop"] = (
            home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
        )
        configs["Cursor"] = (
            home / "Library" / "Application Support" / "Cursor" / "User" / "globalStorage"
            / "cursor.mcp" / "mcp.json"
        )
    elif system == "Windows":
        appdata = Path(os.environ.get("APPDATA", ""))
        if appdata.is_dir():
            configs["Claude Desktop"] = appdata / "Claude" / "claude_desktop_config.json"
            configs["Cursor"] = (
                appdata / "Cursor" / "User" / "globalStorage" / "cursor.mcp" / "mcp.json"
            )
    else:
        # Linux: Claude Desktop not officially supported, but check XDG
        xdg_config = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        configs["Claude Desktop"] = xdg_config / "Claude" / "claude_desktop_config.json"
        configs["Cursor"] = xdg_config / "Cursor" / "User" / "globalStorage" / "cursor.mcp" / "mcp.json"

    return configs


def _detect_claude_code() -> bool:
    """Check if Claude Code CLI is available."""
    return shutil.which("claude") is not None


# --- Config generation ---

def _build_server_config(
    host: str,
    port: str,
    username: str,
    password: str,
    verify_ssl: str,
) -> dict[str, Any]:
    """Build an MCP config that references, but does not contain, credentials."""
    del host, port, username, password, verify_ssl
    python_path = sys.executable
    return {
        "command": python_path,
        "args": ["-m", "cisco_vmanage_mcp"],
        "env": {
            "VMANAGE_CONFIG_FILE": str(CREDENTIALS_FILE),
        },
    }


def _merge_config(existing: dict[str, Any], server_config: dict[str, Any]) -> dict[str, Any]:
    """Merge the cisco-vmanage server config into an existing config file."""
    if "mcpServers" not in existing:
        existing["mcpServers"] = {}
    existing["mcpServers"]["cisco-vmanage"] = server_config
    return existing


# --- Validation ---

def _validate_environment() -> list[str]:
    """Check that Python and the package are installed correctly."""
    errors: list[str] = []

    # Check package is importable
    try:
        import cisco_vmanage_mcp  # noqa: F401
    except ImportError:
        errors.append(
            "cisco-vmanage-mcp package not found. "
            "Install with: pip install -e ."
        )

    return errors


# --- Interactive prompts ---

def _prompt_credentials() -> dict[str, str]:
    """Interactively prompt for vManage credentials."""
    print("\n--- vManage Credentials ---")
    host = input("vManage host [sandbox-sdwan-2.cisco.com]: ").strip() or "sandbox-sdwan-2.cisco.com"
    port = input("vManage port [443]: ").strip() or "443"
    username = input("vManage username: ").strip()
    if not username:
        print("Error: username is required.")
        sys.exit(1)
    password = getpass.getpass("vManage password: ")
    if not password:
        print("Error: password is required.")
        sys.exit(1)
    verify_ssl = input("Verify SSL certificates? (y/N) [N]: ").strip().lower()
    verify_ssl = "true" if verify_ssl in ("y", "yes") else "false"
    return {
        "host": host,
        "port": port,
        "username": username,
        "password": password,
        "verify_ssl": verify_ssl,
    }


def _prompt_client_selection(available: dict[str, Path], claude_code: bool) -> tuple[list[str], bool]:
    """Prompt user to select which MCP clients to configure.

    Returns (list of JSON-config client names, whether to configure Claude Code).
    """
    print("\n--- Detected MCP Clients ---")
    options: list[tuple[str, str]] = []
    idx = 1

    for name, path in available.items():
        exists = path.exists()
        status = "found" if exists else "config dir exists" if path.parent.exists() else "not found"
        print(f"  {idx}. {name} ({status}) - {path}")
        options.append((name, "json"))
        idx += 1

    if claude_code:
        print(f"  {idx}. Claude Code (CLI available)")
        options.append(("Claude Code", "cli"))
        idx += 1

    if not options:
        print("  No MCP clients detected.")
        return [], False

    print(f"  {idx}. Print config JSON (manual copy-paste)")
    options.append(("Manual", "manual"))

    selection = input("\nSelect clients to configure (comma-separated, e.g. 1,2) [all]: ").strip()

    if not selection or selection.lower() == "all":
        indices = list(range(len(options)))
    else:
        try:
            indices = [int(s.strip()) - 1 for s in selection.split(",")]
        except ValueError:
            print("Invalid selection.")
            sys.exit(1)

    json_clients: list[str] = []
    configure_claude_code = False
    manual = False

    for i in indices:
        if 0 <= i < len(options):
            name, kind = options[i]
            if kind == "cli":
                configure_claude_code = True
            elif kind == "manual":
                manual = True
            else:
                json_clients.append(name)

    if manual:
        json_clients.append("__manual__")

    return json_clients, configure_claude_code


# --- Writers ---

def _write_private_text(path: Path, content: str, restrict_parent: bool = False) -> None:
    """Write a regular file without exposing its contents to other users."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if restrict_parent:
        path.parent.chmod(0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            descriptor = -1
            output.write(content)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _dotenv_quote(value: str) -> str:
    """Quote a value for python-dotenv without permitting line injection."""
    if "\n" in value or "\r" in value:
        raise ValueError("Credential values must not contain newlines")
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _write_credentials_file(creds: dict[str, str], dry_run: bool) -> None:
    """Persist vManage settings in the dedicated owner-only dotenv file."""
    values = {
        "VMANAGE_HOST": creds["host"],
        "VMANAGE_PORT": creds["port"],
        "VMANAGE_USERNAME": creds["username"],
        "VMANAGE_PASSWORD": creds["password"],
        "VMANAGE_VERIFY_SSL": creds["verify_ssl"],
    }
    content = "".join(
        f"{name}={_dotenv_quote(value)}\n"
        for name, value in values.items()
    )

    if dry_run:
        print(f"\n[DRY RUN] Would write credentials securely to {CREDENTIALS_FILE}")
        return

    _write_private_text(CREDENTIALS_FILE, content, restrict_parent=True)
    print(f"Wrote credentials securely to {CREDENTIALS_FILE}")

def _write_json_config(path: Path, server_config: dict[str, Any], dry_run: bool) -> None:
    """Write or merge server config into a JSON config file."""
    existing: dict[str, Any] = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            existing = {}

    merged = _merge_config(existing, server_config)
    output = json.dumps(merged, indent=2)

    if dry_run:
        print(f"\n[DRY RUN] Would write to {path}:")
        print(output)
    else:
        _write_private_text(path, output + "\n")
        print(f"Wrote config to {path}")


def _configure_claude_code(creds: dict[str, str], dry_run: bool) -> None:
    """Configure Claude Code via its CLI."""
    del creds

    python_path = sys.executable
    cmd = [
        "claude", "mcp", "add", "cisco-vmanage",
        "-e", f"VMANAGE_CONFIG_FILE={CREDENTIALS_FILE}",
        "--", python_path, "-m", "cisco_vmanage_mcp",
    ]

    if dry_run:
        print("\n[DRY RUN] Would run:")
        print(f"  {' '.join(cmd)}")
    else:
        try:
            # The command is an argv list assembled from constants and trusted local paths.
            result = subprocess.run(  # nosec B603
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                print("Configured Claude Code successfully.")
            else:
                print(f"Claude Code configuration failed: {result.stderr.strip()}")
        except FileNotFoundError:
            print("Error: 'claude' CLI not found. Install Claude Code first.")
        except subprocess.TimeoutExpired:
            print("Error: Claude Code CLI timed out.")


def _print_manual_config(server_config: dict[str, Any]) -> None:
    """Print config JSON for manual copy-paste."""
    manual_block = {
        "mcpServers": {
            "cisco-vmanage": server_config,
        }
    }
    print("\n--- Manual Configuration ---")
    print("Add the following to your MCP client's config file:\n")
    print(json.dumps(manual_block, indent=2))
    print()


# --- Main ---

def main(args: list[str] | None = None) -> None:
    """Entry point for the installer CLI."""
    parser = argparse.ArgumentParser(
        prog="vmanage-mcp install",
        description="Configure MCP clients to use cisco-vmanage-mcp server",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be written without modifying any files",
    )
    parsed = parser.parse_args(args)
    dry_run: bool = parsed.dry_run

    print("cisco-vmanage-mcp installer")
    print("=" * 40)

    # Validate environment
    errors = _validate_environment()
    if errors:
        print("\nEnvironment validation failed:")
        for err in errors:
            print(f"  - {err}")
        sys.exit(1)
    print("Environment OK: Python and package validated.")

    # Detect clients
    client_configs = _get_client_configs()
    claude_code = _detect_claude_code()

    # Prompt for client selection
    json_clients, configure_cc = _prompt_client_selection(client_configs, claude_code)

    if not json_clients and not configure_cc:
        print("No clients selected. Exiting.")
        return

    # Prompt for credentials
    creds = _prompt_credentials()
    _write_credentials_file(creds, dry_run)

    # Build config
    server_config = _build_server_config(
        host=creds["host"],
        port=creds["port"],
        username=creds["username"],
        password=creds["password"],
        verify_ssl=creds["verify_ssl"],
    )

    # Write configs
    for client_name in json_clients:
        if client_name == "__manual__":
            _print_manual_config(server_config)
        elif client_name in client_configs:
            _write_json_config(client_configs[client_name], server_config, dry_run)

    if configure_cc:
        _configure_claude_code(creds, dry_run)

    if not dry_run:
        print("\nDone! Restart your MCP client(s) to pick up the new configuration.")
    else:
        print("\n[DRY RUN] No files were modified.")


if __name__ == "__main__":
    main()
