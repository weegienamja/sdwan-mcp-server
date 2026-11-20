"""Exercise the laptop session without a work network or model download."""

import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from cisco_vmanage_mcp import workstation
from cisco_vmanage_mcp.services.local_model import LocalModel, LocalModelError
from cisco_vmanage_mcp.services.workstation import ManagerReader, ManagerReadError, manager_origin


@pytest.mark.parametrize("bad", [
    "http://manager", "https://user@manager", "manager/dataservice", "manager?tenant=1",
    "https://manager/#token", "manager\\other", "manager:invalid", "https://bad host",
])
def test_manager_requires_an_https_origin(bad):
    with pytest.raises(ValueError):
        manager_origin(bad)


def test_manager_accepts_nonstandard_https_port():
    url = manager_origin("manager.internal:8443")
    assert (url.scheme, url.host, url.port) == ("https", "manager.internal", 8443)


@pytest.mark.asyncio
async def test_bearer_never_leaves_manager_or_follows_redirects():
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": "https://other.invalid/login"})

    reader = ManagerReader("manager.invalid", "operator-fixture", transport=httpx.MockTransport(handle))
    try:
        for path in ("https://other.invalid", "//other.invalid", "/dataservice/device/action", "/logout"):
            with pytest.raises(ManagerReadError, match="outside"):
                await reader.get(path)
        with pytest.raises(ManagerReadError, match="sign-in"):
            await reader.get("/dataservice/device")
        assert reader.sign_in_required
    finally:
        await reader.close()
    assert [(r.method, r.url.host, r.headers["Authorization"]) for r in calls] == [
        ("GET", "manager.invalid", "Bearer operator-fixture"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "body", "error"), [
    (401, {}, "sign-in"), (403, {}, "role"), (503, {}, "503"), (200, {}, "evidence table"),
])
async def test_read_failures_are_visible(status, body, error):
    reader = ManagerReader("manager.invalid", "fixture", transport=httpx.MockTransport(
        lambda request: httpx.Response(status, json=body),
    ))
    try:
        with pytest.raises(ManagerReadError, match=error):
            await reader.get("/dataservice/device")
    finally:
        await reader.close()


@pytest.mark.asyncio
async def test_operator_chat_reads_scoped_inventory_and_sends_only_evidence(monkeypatch, capsys):
    def manager(request):
        assert request.method == "GET"
        assert request.url.path == "/dataservice/device"
        assert request.headers["Authorization"] == "Bearer example-personal-jwt"
        return httpx.Response(200, json={"data": [
            {"site-id": 100, "host-name": "edge-visible", "reachability": "unreachable"},
            {"site-id": 200, "host-name": "other-site", "reachability": "unreachable"},
        ]})

    def model(request):
        assert request.method == "POST"
        assert request.url.path == "/v1/chat/completions"
        assert request.url.host == "127.0.0.1"
        assert "example-personal-jwt" not in str(request.headers) + request.content.decode()
        payload = json.loads(request.content)
        assert "tools" not in payload
        assert "edge-visible" in payload["messages"][1]["content"]
        assert "other-site" not in payload["messages"][1]["content"]
        return httpx.Response(200, json={"choices": [{"message": {"content": "edge-visible is unreachable."}}]})

    prompts = iter(["Which devices at site 100 are unreachable?", "/quit"])
    monkeypatch.setattr(workstation.click, "prompt", lambda *a, **kw: next(prompts))
    reader = ManagerReader("manager.invalid", "example-personal-jwt", transport=httpx.MockTransport(manager))
    try:
        async with httpx.AsyncClient(base_url="http://127.0.0.1", transport=httpx.MockTransport(model)) as client:
            await workstation._chat(reader, LocalModel(client, "qwen"))
    finally:
        await reader.close()
    output = capsys.readouterr().out
    assert "2 devices visible" in output
    assert "edge-visible is unreachable" in output
    assert "GET /dataservice/device" in output
    assert "example-personal-jwt" not in output


@pytest.mark.asyncio
async def test_model_failure_does_not_trigger_another_provider():
    async with httpx.AsyncClient(base_url="http://127.0.0.1", transport=httpx.MockTransport(
        lambda _: httpx.Response(500),
    )) as client:
        model = LocalModel(client, "qwen")
        with pytest.raises(LocalModelError):
            await model.explain("What is down?", {})


@pytest.mark.asyncio
async def test_alarm_questions_filter_by_site_before_limiting_the_display():
    client = AsyncMock(spec=ManagerReader)
    client.get.side_effect = [
        {"data": [{"system-ip": "192.0.2.8", "severity": "Critical"}] * 60
                 + [{"site-id": 200, "severity": "Critical"}]},
        {"data": [{"site-id": 100, "system-ip": "192.0.2.8"}]},
    ]
    response = await workstation.EvidenceAssistant(client).ask("Which alarms are critical at site 100?")
    assert response["data"]["count"] == 60
    assert len(response["data"]["items"]) == 50
    assert len(response["evidence"]) == 2


@pytest.mark.asyncio
async def test_expired_identity_stops_even_when_correlation_returns_partial_data(monkeypatch):
    reader = AsyncMock(spec=ManagerReader)
    reader.get.side_effect = [
        {"data": []}, ManagerReadError("expired"), ManagerReadError("expired"),
    ]
    reader.sign_in_required = True
    monkeypatch.setattr(workstation.click, "prompt", lambda *a, **kw: "How healthy is the fabric?")
    model = AsyncMock(spec=LocalModel)
    with pytest.raises(ManagerReadError, match="expired"):
        await workstation._chat(reader, model)
    model.explain.assert_not_called()


def test_selects_the_first_model_shard_and_ignores_projectors(tmp_path: Path):
    for name in ("mmproj-Qwen3.5-4B.gguf", "mtp-Qwen3.5-4B.gguf",
                 "Qwen3.5-4B-00001-of-00002.gguf", "Qwen3.5-4B-00002-of-00002.gguf"):
        (tmp_path / name).touch()
    assert workstation._choose_model(tmp_path).name == "Qwen3.5-4B-00001-of-00002.gguf"
