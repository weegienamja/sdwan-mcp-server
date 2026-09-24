"""Contracts for guided local vManage setup."""

from __future__ import annotations

import pytest
from dotenv import dotenv_values
from pydantic import ValidationError

from cisco_vmanage_mcp.services.setup import LocalSetupService, VManageSetupInput


class SetupClient:
    host = "customer-vmanage.example.test"
    port = "443"

    def __init__(self) -> None:
        self.authenticated = False
        self.closed = False

    async def authenticate(self) -> None:
        self.authenticated = True

    async def get(self, endpoint: str, params=None) -> dict:
        if endpoint == "/dataservice/device":
            return {
                "data": [
                    {
                        "device-type": "vedge",
                        "device-model": "C8500-12X",
                        "version": "17.18.1",
                        "system-ip": "10.0.0.1",
                        "reachability": "reachable",
                    }
                ]
            }
        return {"data": []}

    async def close(self) -> None:
        self.closed = True


def _values(**updates) -> VManageSetupInput:
    values = {
        "host": "customer-vmanage.example.test",
        "port": 443,
        "username": "operator",
        "password": "super-secret",
        "verify_ssl": True,
        **updates,
    }
    return VManageSetupInput.model_validate(values)


def test_setup_input_rejects_urls_and_incompatible_tls_options() -> None:
    with pytest.raises(ValidationError, match="without https"):
        _values(host="https://vmanage.example.test")
    with pytest.raises(ValidationError, match="CA bundle"):
        _values(verify_ssl=False, ca_bundle="/tmp/private-ca.pem")


@pytest.mark.asyncio
async def test_setup_test_returns_full_qualification_and_closes(tmp_path) -> None:
    clients: list[SetupClient] = []

    def factory(_values: VManageSetupInput) -> SetupClient:
        client = SetupClient()
        clients.append(client)
        return client

    service = LocalSetupService(tmp_path / "vmanage.env", client_factory=factory)

    report = await service.test(_values())

    assert report.ready is True
    assert report.device_count == 1
    assert report.device_models == {"C8500-12X": 1}
    assert len(report.capabilities) == 12
    assert clients[0].authenticated is True
    assert clients[0].closed is True


def test_setup_save_is_private_atomic_and_never_exposed_by_status(tmp_path) -> None:
    config_file = tmp_path / "private" / "vmanage.env"
    service = LocalSetupService(config_file)

    service.save(_values())
    saved = dotenv_values(config_file)
    status = service.status()

    assert saved["VMANAGE_HOST"] == "customer-vmanage.example.test"
    assert saved["VMANAGE_USERNAME"] == "operator"
    assert saved["VMANAGE_PASSWORD"] == "super-secret"
    assert saved["VMANAGE_VERIFY_SSL"] == "true"
    assert config_file.stat().st_mode & 0o777 == 0o600
    assert config_file.parent.stat().st_mode & 0o777 == 0o700
    assert status.credentials_configured is True
    assert "operator" not in status.model_dump_json()
    assert "super-secret" not in status.model_dump_json()