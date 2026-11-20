import base64
import hashlib
import io
import json
import struct
from contextlib import AsyncExitStack, asynccontextmanager
from importlib.resources import files
from unittest.mock import AsyncMock

import pytest

from cisco_vmanage_mcp import edge_host
from cisco_vmanage_mcp.edge_snapshot import READS, question_evidence, validate_capture
from cisco_vmanage_mcp.services.workstation import _READ_PATHS


def capture():
    return {
        "captured": "2026-11-20T18:05:00Z", "selected": None,
        "tables": {
            "inventory": {"state": "ok", "rows": [
                {"system-ip": "192.0.2.1", "site-id": 100, "host-name": "west", "reachability": "unreachable"},
                {"system-ip": "192.0.2.2", "site-id": 200, "host-name": "east", "reachability": "reachable"},
            ]},
            "counts": {"state": "denied", "rows": []}, "alarms": {"state": "denied", "rows": []},
        },
    }


def test_packaged_extension_identity_permissions_and_read_paths():
    extension = files("cisco_vmanage_mcp").joinpath("edge_addon")
    manifest = json.loads(extension.joinpath("manifest.json").read_text("utf-8"))
    digest = hashlib.sha256(base64.b64decode(manifest["key"])).hexdigest()
    extension_id = "".join(chr(ord("a") + int(d, 16)) for d in digest[:32])
    assert edge_host.EXTENSION == f"chrome-extension://{extension_id}/"
    assert set(manifest["permissions"]) == {"activeTab", "scripting", "nativeMessaging", "sidePanel"}
    assert manifest["side_panel"] == {"default_path": "diagnose.html"}
    assert "host_permissions" not in manifest
    assert "externally_connectable" not in manifest
    assert {r["path"] for r in READS.values()} == _READ_PATHS


def test_question_evidence_filters_sites_and_keeps_denied_counts_unknown():
    snapshot = validate_capture(capture())
    data = question_evidence(snapshot, "Which devices are unreachable at site 100?")
    assert data["matching_device_count_in_capture"] == 1
    assert data["matching_alarm_count_in_capture"] is None
    assert data["fabric_alarm_counts"] is None
    assert data["device_sample"][0]["host-name"] == "west"
    assert "east" not in json.dumps(data)


@pytest.mark.parametrize("extra", ["cookie", "password", "Authorization", "running-config"])
def test_rejects_unreviewed_fields(extra):
    snapshot = capture()
    snapshot["tables"]["inventory"]["rows"][0][extra] = "private"
    with pytest.raises(ValueError, match="fields"):
        validate_capture(snapshot)


@pytest.mark.parametrize("frame", [b"\x01", struct.pack("<I", 0), struct.pack("<I", edge_host.MAX_FRAME + 1),
                                   struct.pack("<I", 100) + b"{}", struct.pack("<I", 2) + b"[]"])
def test_rejects_invalid_native_frames(frame):
    with pytest.raises(ValueError):
        edge_host.read_frame(io.BytesIO(frame))


@pytest.mark.asyncio
async def test_native_chat_discards_failed_refresh_and_reuses_only_local_model(tmp_path, monkeypatch):
    fake = AsyncMock()
    fake.explain.return_value = "West is unreachable."

    @asynccontextmanager
    async def run_model(*args):
        yield fake

    monkeypatch.setattr(edge_host, "run_local_model", run_model)
    manager = "https://manager.invalid"
    valid = {"type": "capture", "origin": manager, "snapshot": capture()}
    invalid = {**valid, "origin": "https://other.invalid"}
    question = {"type": "ask", "question": "What about site 100?"}
    source, sink = io.BytesIO(), io.BytesIO()
    for message in ({"type": "hello"}, valid, question, invalid, question, valid, {"type": "clear"}, question):
        edge_host.write_frame(source, message)
    source.seek(0)
    await edge_host.serve(tmp_path, {"manager": manager, "model": str(tmp_path / "model.gguf")}, source, sink)
    sink.seek(0)
    replies = []
    while (result := edge_host.read_frame(sink)) is not None:
        replies.append(result["type"])
    assert replies == ["hello", "ready", "answer", "error", "error", "ready", "cleared", "error"]
    fake.explain.assert_awaited_once()


def test_device_detail_is_scoped_to_visible_system_ip():
    snapshot = capture()
    snapshot["selected"] = "192.0.2.1"
    snapshot["tables"].update({s: {"state": "ok", "rows": []} for s in ("bfd", "control", "system")})
    snapshot = validate_capture(snapshot)
    assert question_evidence(snapshot, "Inspect 192.0.2.1")["device_detail"]["system_ip"] == "192.0.2.1"
    assert "device_detail" not in question_evidence(snapshot, "Inspect site 200")
    snapshot["selected"] = "192.0.2.3"
    with pytest.raises(ValueError, match="outside"):
        validate_capture(snapshot)


@pytest.mark.asyncio
async def test_mode_switch_discards_snapshot_and_accepts_only_selected_origin(tmp_path):
    work, lab = "https://work.invalid", "https://lab.invalid:8443"
    settings = {"manager": work, "sandbox": lab, "model": "unused"}
    session = edge_host.DiagnosticSession(tmp_path, settings, AsyncExitStack())
    assert (await session.handle({"type": "hello"}))["origins"] == {"work": work, "sandbox": lab}
    for mode, origin, other in (("work", work, lab), ("sandbox", lab, work), ("work", work, lab)):
        assert (await session.handle({"type": "select", "mode": mode}))["origin"] == origin
        with pytest.raises(ValueError, match="Capture"):
            await session.handle({"type": "ask", "question": "List devices"})
        with pytest.raises(ValueError, match="origin"):
            await session.handle({"type": "capture", "origin": other, "snapshot": capture()})
        assert (await session.handle({"type": "capture", "origin": origin, "snapshot": capture()}))["type"] == "ready"
