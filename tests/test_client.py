"""Tests for the authenticated vManage HTTP client."""

from __future__ import annotations

import asyncio
import os
import ssl
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock

import httpx
import pytest

from cisco_vmanage_mcp.client import (
	AuthenticationError,
	ConfigurationError,
	NotFoundError,
	RateLimitError,
	VManageAPIError,
	VManageClient,
	_load_configuration_environment,
)


class AuthenticationClient:
	"""Minimal async client used to exercise the authentication handshake."""

	def __init__(
		self,
		login_response: httpx.Response | None = None,
		token_response: httpx.Response | None = None,
	) -> None:
		self.login_response = login_response or httpx.Response(200, text="")
		self.token_response = token_response or httpx.Response(200, text="token-1")
		self.headers: dict[str, str] = {}
		self.login_calls = 0
		self.token_calls = 0

	async def post(self, *_args, **_kwargs) -> httpx.Response:
		self.login_calls += 1
		await asyncio.sleep(0)
		return self.login_response

	async def get(self, *_args, **_kwargs) -> httpx.Response:
		self.token_calls += 1
		return self.token_response


class ResponseQueueClient:
	"""Minimal async client returning queued GET responses or exceptions."""

	def __init__(self, *responses: httpx.Response | Exception) -> None:
		self.responses = list(responses)
		self.headers: dict[str, str] = {}
		self.get_calls = 0

	async def get(self, *_args, **_kwargs) -> httpx.Response:
		self.get_calls += 1
		response = self.responses.pop(0)
		if isinstance(response, Exception):
			raise response
		return response


def _set_http_client(client: VManageClient, transport: object) -> None:
	client._client = cast(Any, transport)


@pytest.fixture
def configured_environment(monkeypatch):
	monkeypatch.setenv("VMANAGE_USERNAME", "audit-user")
	monkeypatch.setenv("VMANAGE_PASSWORD", "audit-password")
	monkeypatch.setenv("VMANAGE_HOST", "vmanage.example.test")
	monkeypatch.delenv("VMANAGE_VERIFY_SSL", raising=False)
	monkeypatch.delenv("VMANAGE_MAX_RETRIES", raising=False)
	monkeypatch.delenv("VMANAGE_CA_BUNDLE", raising=False)


def test_tls_verification_is_enabled_by_default(configured_environment) -> None:
	client = VManageClient()

	assert client.verify_ssl is True


def test_explicit_connection_settings_override_environment(configured_environment) -> None:
	client = VManageClient(
		host="customer-vmanage.example.test",
		port=8443,
		username="setup-user",
		password="setup-password",
		verify_ssl=False,
		max_retries=1,
	)

	assert client.base_url == "https://customer-vmanage.example.test:8443"
	assert client.username == "setup-user"
	assert client.password == "setup-password"
	assert client.verify_ssl is False
	assert client.max_retries == 1


def test_private_config_overrides_project_dotenv_but_not_external_env(
	monkeypatch,
	tmp_path,
) -> None:
	project_file = tmp_path / ".env"
	config_file = tmp_path / "private" / "vmanage.env"
	config_file.parent.mkdir()
	project_file.write_text(
		"VMANAGE_HOST=project.example.test\nVMANAGE_USERNAME=project-user\n",
		encoding="utf-8",
	)
	config_file.write_text(
		"VMANAGE_HOST=private.example.test\nVMANAGE_USERNAME=private-user\n",
		encoding="utf-8",
	)
	monkeypatch.setenv("VMANAGE_CONFIG_FILE", str(config_file))
	monkeypatch.setenv("VMANAGE_HOST", "external.example.test")
	monkeypatch.delenv("VMANAGE_USERNAME", raising=False)

	selected = _load_configuration_environment(project_file)

	assert selected == config_file
	assert os.environ["VMANAGE_HOST"] == "external.example.test"
	assert os.environ["VMANAGE_USERNAME"] == "private-user"


def test_tls_verification_can_be_explicitly_disabled(
	configured_environment,
	monkeypatch,
) -> None:
	monkeypatch.setenv("VMANAGE_VERIFY_SSL", "false")

	client = VManageClient()

	assert client.verify_ssl is False


def test_invalid_tls_verification_value_is_rejected(
	configured_environment,
	monkeypatch,
) -> None:
	monkeypatch.setenv("VMANAGE_VERIFY_SSL", "tru")

	with pytest.raises(ConfigurationError, match="VMANAGE_VERIFY_SSL"):
		VManageClient()


@pytest.mark.parametrize("value", ["-1", "11", "not-a-number"])
def test_invalid_retry_count_is_rejected(
	configured_environment,
	monkeypatch,
	value,
) -> None:
	monkeypatch.setenv("VMANAGE_MAX_RETRIES", value)

	with pytest.raises(ConfigurationError, match="VMANAGE_MAX_RETRIES"):
		VManageClient()


def test_custom_ca_bundle_builds_verified_context(
	configured_environment,
	monkeypatch,
) -> None:
	bundle = str(Path(__file__).resolve().parents[1] / "certs" / "ca-bundle.pem")
	monkeypatch.setenv("VMANAGE_CA_BUNDLE", bundle)

	client = VManageClient()

	assert client.verify_ssl is True
	assert isinstance(client.tls_verify, ssl.SSLContext)


@pytest.mark.asyncio
async def test_authentication_rejects_login_http_error(configured_environment) -> None:
	client = VManageClient()
	_set_http_client(
		client,
		AuthenticationClient(login_response=httpx.Response(500, text="failure")),
	)

	with pytest.raises(AuthenticationError, match="HTTP 500"):
		await client.authenticate()


@pytest.mark.asyncio
async def test_authentication_rejects_empty_xsrf_token(configured_environment) -> None:
	client = VManageClient()
	_set_http_client(
		client,
		AuthenticationClient(token_response=httpx.Response(200, text="  ")),
	)

	with pytest.raises(AuthenticationError, match="empty XSRF token"):
		await client.authenticate()


@pytest.mark.asyncio
async def test_concurrent_authentication_performs_one_handshake(configured_environment) -> None:
	client = VManageClient()
	transport = AuthenticationClient()
	_set_http_client(client, transport)

	await asyncio.gather(*(client.authenticate() for _ in range(10)))

	assert transport.login_calls == 1
	assert transport.token_calls == 1
	assert client._token == "token-1"


def test_client_does_not_expose_arbitrary_post(configured_environment) -> None:
	client = VManageClient()

	assert not hasattr(client, "post")


@pytest.mark.asyncio
async def test_rate_limit_retry_uses_retry_after(
	configured_environment,
	monkeypatch,
) -> None:
	client = VManageClient()
	client._token = "token-1"
	_set_http_client(client, ResponseQueueClient(
		httpx.Response(429, headers={"Retry-After": "2"}),
		httpx.Response(200, json={"data": []}),
	))
	delays: list[float] = []

	async def record_sleep(delay: float) -> None:
		delays.append(delay)

	monkeypatch.setattr(asyncio, "sleep", record_sleep)

	result = await client.get("/dataservice/device")

	assert result == {"data": []}
	assert delays == [2.0]


@pytest.mark.asyncio
async def test_exhausted_rate_limit_raises_typed_error(
	configured_environment,
	monkeypatch,
) -> None:
	monkeypatch.setenv("VMANAGE_MAX_RETRIES", "1")
	client = VManageClient()
	client._token = "token-1"
	_set_http_client(
		client,
		ResponseQueueClient(httpx.Response(429), httpx.Response(429)),
	)
	monkeypatch.setattr(asyncio, "sleep", AsyncMock())

	with pytest.raises(RateLimitError):
		await client.get("/dataservice/device")


@pytest.mark.asyncio
async def test_not_found_raises_typed_error(configured_environment) -> None:
	client = VManageClient()
	client._token = "token-1"
	_set_http_client(client, ResponseQueueClient(httpx.Response(404)))

	with pytest.raises(NotFoundError):
		await client.get("/dataservice/missing")


@pytest.mark.asyncio
async def test_expired_session_reauthenticates_once(configured_environment) -> None:
	client = VManageClient()
	client._token = "stale-token"
	_set_http_client(client, ResponseQueueClient(
		httpx.Response(403),
		httpx.Response(200, json={"data": [1]}),
	))
	client.reauthenticate = AsyncMock()

	result = await client.get("/dataservice/device")

	assert result == {"data": [1]}
	client.reauthenticate.assert_awaited_once_with(stale_token="stale-token")


@pytest.mark.asyncio
async def test_non_json_response_raises_api_error(configured_environment) -> None:
	client = VManageClient()
	client._token = "token-1"
	_set_http_client(client, ResponseQueueClient(
		httpx.Response(200, text="<html>login page</html>"),
	))

	with pytest.raises(VManageAPIError, match="invalid JSON"):
		await client.get("/dataservice/device")
