"""Guided local setup for connecting the browser to Cisco Catalyst SD-WAN."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Protocol
from uuid import uuid4

import idna
from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from cisco_vmanage_mcp.client import VManageClient
from cisco_vmanage_mcp.services.qualification import (
    EnvironmentQualification,
    qualify_environment,
)

SETUP_SCHEMA_VERSION = 1


class SetupError(RuntimeError):
    """Raised when local setup cannot be tested or persisted safely."""


class ConnectedSetupClient(Protocol):
    """Connected client operations returned to the browser application."""

    async def get(self, endpoint: str, params: dict | None = None) -> dict: ...

    async def close(self) -> None: ...


class SetupClient(ConnectedSetupClient, Protocol):
    """Additional authentication operation used while testing setup."""

    host: str
    port: str

    async def authenticate(self) -> None: ...


class SetupService(Protocol):
    """Guided setup operations consumed by the browser application."""

    def status(self) -> SetupConnectionStatus: ...

    async def test(self, values: VManageSetupInput) -> EnvironmentQualification: ...

    async def connect(
        self,
        values: VManageSetupInput,
    ) -> tuple[ConnectedSetupClient, EnvironmentQualification]: ...

    def save(self, values: VManageSetupInput) -> None: ...


class VManageSetupInput(BaseModel):
    """Validated connection values accepted from the local setup form."""

    model_config = ConfigDict(extra="forbid")

    host: str = Field(min_length=1, max_length=253)
    port: int = Field(default=443, ge=1, le=65_535)
    username: str = Field(min_length=1, max_length=256)
    password: SecretStr = Field(min_length=1, max_length=1_024)
    verify_ssl: bool = True
    ca_bundle: str | None = Field(default=None, max_length=2_048)

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        host = value.strip().rstrip(".")
        if (
            not host
            or "://" in host
            or "/" in host
            or "@" in host
            or any(character.isspace() for character in host)
        ):
            raise ValueError("Enter a hostname or IPv4 address without https:// or a path")
        try:
            return idna.encode(host).decode("ascii")
        except idna.IDNAError as exc:
            raise ValueError("Enter a valid hostname or IPv4 address") from exc

    @field_validator("username")
    @classmethod
    def normalize_username(cls, value: str) -> str:
        username = value.strip()
        if not username or "\n" in username or "\r" in username:
            raise ValueError("Username is required and must be one line")
        return username

    @field_validator("ca_bundle")
    @classmethod
    def normalize_ca_bundle(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @model_validator(mode="after")
    def validate_tls_options(self) -> VManageSetupInput:
        if self.ca_bundle and not self.verify_ssl:
            raise ValueError("A CA bundle can be used only when TLS verification is enabled")
        return self


class SetupConnectionStatus(BaseModel):
    """Non-secret summary of the saved or environment-provided connection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = SETUP_SCHEMA_VERSION
    credentials_configured: bool
    config_file_exists: bool
    config_file: str
    host: str
    port: int
    verify_ssl: bool
    ca_bundle_configured: bool


SetupClientFactory = Callable[[VManageSetupInput], SetupClient]


def _default_client_factory(values: VManageSetupInput) -> SetupClient:
    return VManageClient(
        host=values.host,
        port=values.port,
        username=values.username,
        password=values.password.get_secret_value(),
        verify_ssl=values.verify_ssl,
        ca_bundle=values.ca_bundle,
    )


def _dotenv_quote(value: str) -> str:
    if "\n" in value or "\r" in value:
        raise SetupError("Configuration values must not contain newlines")
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


class LocalSetupService:
    """Test and persist one local vManage connection without exposing secrets."""

    def __init__(
        self,
        config_file: Path | None = None,
        *,
        client_factory: SetupClientFactory = _default_client_factory,
    ) -> None:
        self.config_file = (
            config_file
            or Path(
                os.getenv(
                    "VMANAGE_CONFIG_FILE",
                    str(Path.home() / ".vmanage-mcp" / "vmanage.env"),
                )
            )
        ).expanduser()
        self._client_factory = client_factory

    def status(self) -> SetupConnectionStatus:
        """Return current connection metadata without usernames or secrets."""
        configured = {
            key: value
            for key, value in dotenv_values(self.config_file).items()
            if isinstance(key, str) and isinstance(value, str)
        } if self.config_file.is_file() else {}
        host = os.getenv("VMANAGE_HOST") or configured.get(
            "VMANAGE_HOST", "sandbox-sdwan-2.cisco.com"
        )
        raw_port = os.getenv("VMANAGE_PORT") or configured.get("VMANAGE_PORT", "443")
        raw_verify = os.getenv("VMANAGE_VERIFY_SSL") or configured.get(
            "VMANAGE_VERIFY_SSL", "true"
        )
        username = os.getenv("VMANAGE_USERNAME") or configured.get("VMANAGE_USERNAME")
        password = os.getenv("VMANAGE_PASSWORD") or configured.get("VMANAGE_PASSWORD")
        ca_bundle = os.getenv("VMANAGE_CA_BUNDLE") or configured.get("VMANAGE_CA_BUNDLE")
        try:
            port = int(raw_port)
        except (TypeError, ValueError):
            port = 443
        return SetupConnectionStatus(
            credentials_configured=bool(username and password),
            config_file_exists=self.config_file.is_file(),
            config_file=str(self.config_file),
            host=host,
            port=port if 1 <= port <= 65_535 else 443,
            verify_ssl=str(raw_verify).strip().lower() != "false",
            ca_bundle_configured=bool(ca_bundle),
        )

    async def test(self, values: VManageSetupInput) -> EnvironmentQualification:
        """Authenticate and run the full read-only qualification, then disconnect."""
        client, report = await self.connect(values)
        await client.close()
        return report

    async def connect(
        self,
        values: VManageSetupInput,
    ) -> tuple[SetupClient, EnvironmentQualification]:
        """Return an authenticated client and its detailed qualification report."""
        client = self._client_factory(values)
        try:
            await client.authenticate()
            report = await qualify_environment(client)
        except Exception:
            await client.close()
            raise
        return client, report

    def save(self, values: VManageSetupInput) -> None:
        """Atomically persist validated connection settings in an owner-only file."""
        existing = {
            key: value
            for key, value in dotenv_values(self.config_file).items()
            if isinstance(key, str) and isinstance(value, str)
        } if self.config_file.is_file() else {}
        existing.update({
            "VMANAGE_HOST": values.host,
            "VMANAGE_PORT": str(values.port),
            "VMANAGE_USERNAME": values.username,
            "VMANAGE_PASSWORD": values.password.get_secret_value(),
            "VMANAGE_VERIFY_SSL": "true" if values.verify_ssl else "false",
        })
        if values.ca_bundle:
            existing["VMANAGE_CA_BUNDLE"] = values.ca_bundle
        else:
            existing.pop("VMANAGE_CA_BUNDLE", None)
        content = "".join(
            f"{key}={_dotenv_quote(value)}\n"
            for key, value in sorted(existing.items())
        )
        self._write_private(content)

    def _write_private(self, content: str) -> None:
        self.config_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            self.config_file.parent.chmod(0o700)
        temporary = self.config_file.with_name(
            f".{self.config_file.name}.{uuid4().hex}.tmp"
        )
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                descriptor = -1
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.config_file)
            if os.name != "nt":
                self.config_file.chmod(0o600)
        except OSError as exc:
            raise SetupError("Unable to save local connection settings") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)