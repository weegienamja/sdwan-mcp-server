"""Edge Native Messaging host and per-user registration for the Windows kit."""

import argparse
import asyncio
import json
import os
import struct
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import BinaryIO

from cisco_vmanage_mcp.edge_snapshot import question_evidence, validate_capture
from cisco_vmanage_mcp.services.local_model import LocalModelError, run_local_model
from cisco_vmanage_mcp.services.workstation import manager_origin
from cisco_vmanage_mcp.utils.private_files import write_private_text

HOST = "org.sdwan.workstation"
EXTENSION = "chrome-extension://beapjnblokiomlppfhbidaaabofbolci/"
MAX_FRAME = 4 * 1024 * 1024


def settings_dir() -> Path:
    return Path(os.environ["LOCALAPPDATA"]) / "SD-WAN Workstation"


def read_frame(stream: BinaryIO) -> dict | None:
    header = stream.read(4)
    if not header:
        return None
    if len(header) != 4:
        raise ValueError("Incomplete frame.")
    size, = struct.unpack("<I", header)
    if not 0 < size <= MAX_FRAME:
        raise ValueError("Invalid frame size.")
    payload = stream.read(size)
    if len(payload) != size:
        raise ValueError("Incomplete frame.")
    data = json.loads(payload)
    if not isinstance(data, dict):
        raise ValueError("Expected a message.")
    return data


def write_frame(stream: BinaryIO, data: dict) -> None:
    payload = json.dumps(data, ensure_ascii=True, allow_nan=False).encode("ascii")
    stream.write(struct.pack("<I", len(payload)) + payload)
    stream.flush()


def install(kit: Path, origin: str, gguf: Path) -> None:
    if sys.platform != "win32":
        raise OSError("The Edge workstation kit requires Windows.")
    import winreg

    origin = str(manager_origin(origin)).rstrip("/")
    gguf = gguf.expanduser().resolve(strict=True)
    with gguf.open("rb") as file:
        if file.read(4) != b"GGUF":
            raise ValueError("Choose a GGUF model file.")
    exe = (kit / "sdwan-edge-host.exe").resolve(strict=True)
    directory = settings_dir()
    manifest = directory / "edge-host.json"
    write_private_text(directory / "settings.json", json.dumps({"manager": origin, "model": str(gguf)}))
    write_private_text(manifest, json.dumps({
        "name": HOST, "description": "Local read-only SD-WAN diagnostics", "type": "stdio",
        "path": str(exe), "allowed_origins": [EXTENSION],
    }))
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"Software\Microsoft\Edge\NativeMessagingHosts\{HOST}") as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, str(manifest))


class DiagnosticSession:
    def __init__(self, kit: Path, settings: dict, stack: AsyncExitStack):
        self.kit = kit
        self.settings = settings
        self.stack = stack
        self.snapshot = None
        self.model = None

    async def handle(self, message: dict) -> dict:
        kind = message.get("type")
        if kind == "hello":
            return {"type": "hello", "manager": self.settings["manager"]}
        if kind == "clear":
            self.snapshot = None
            return {"type": "cleared"}
        if kind == "capture":
            self.snapshot = None
            if message.get("origin") != self.settings["manager"]:
                raise ValueError("Unexpected Manager origin.")
            self.snapshot = validate_capture(message["snapshot"])
            return {"type": "ready"}
        if kind != "ask" or self.snapshot is None:
            raise ValueError("Capture a Manager snapshot before asking a question.")
        question = message.get("question")
        if not isinstance(question, str) or not 1 <= len(question.strip()) <= 2000:
            raise ValueError("Enter a question of up to 2,000 characters.")
        if self.model is None:
            self.model = await self.stack.enter_async_context(run_local_model(
                self.kit / "llama" / "llama-server.exe", Path(self.settings["model"]),
            ))
        answer = await self.model.explain(question, question_evidence(self.snapshot, question))
        return {"type": "answer", "answer": answer[:16000]}


async def serve(kit: Path, settings: dict, source: BinaryIO, sink: BinaryIO) -> None:
    async with AsyncExitStack() as stack:
        session = DiagnosticSession(kit, settings, stack)
        while (message := await asyncio.to_thread(read_frame, source)) is not None:
            try:
                reply = await session.handle(message)
            except (ValueError, LocalModelError) as exc:
                reply = {"type": "error", "error": str(exc)}
            except (KeyError, TypeError, OSError):
                reply = {"type": "error", "error": "Check the workstation installation and GGUF path."}
            write_frame(sink, reply)


def main() -> int:
    kit = Path(sys.executable).resolve().parent.parent
    if len(sys.argv) > 1 and sys.argv[1] == "--setup":
        from cisco_vmanage_mcp.workstation import _choose_model

        parser = argparse.ArgumentParser()
        parser.add_argument("--manager")
        parser.add_argument("--model", type=Path)
        options = parser.parse_args(sys.argv[2:])
        origin = options.manager or input("Cisco Manager HTTPS hostname: ").strip()
        model = options.model or _choose_model(Path.home() / "Downloads")
        try:
            install(kit, origin, model)
        except (ValueError, OSError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print("Open edge://extensions and load the Edge extension folder from this kit.")
        print(kit / "Edge extension")
        return 0
    if len(sys.argv) < 2 or sys.argv[1] != EXTENSION:
        return 1
    try:
        settings = json.loads((settings_dir() / "settings.json").read_text("utf-8"))
        asyncio.run(serve(kit, settings, sys.stdin.buffer, sys.stdout.buffer))
    except (OSError, ValueError, EOFError):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
