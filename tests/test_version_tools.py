"""Tests for the external version-check boundary."""

from __future__ import annotations

import httpx
import pytest

from cisco_vmanage_mcp.tools import version_tools


@pytest.mark.asyncio
async def test_version_check_verifies_tls(monkeypatch) -> None:
    options: dict = {}

    class FakeClient:
        def __init__(self, **kwargs) -> None:
            options.update(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def get(self, _url: str) -> httpx.Response:
            return httpx.Response(200, json=[{"name": "v1.0.0"}])

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    assert await version_tools._fetch_latest_tag() == "1.0.0"
    assert options["verify"] is True
