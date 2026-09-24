"""Validated deployment settings for local and enterprise browser modes."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import threading
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from cisco_vmanage_mcp.services.persistence import (
    canonical_json_bytes,
    prepare_private_directory,
)

AuthMode = Literal["local", "oidc"]
CsvTuple = Annotated[tuple[str, ...], NoDecode]
_STATE_BINDING_LOCK = threading.Lock()


def is_loopback_host(host: str) -> bool:
    """Return whether a bind host is restricted to the local machine."""
    normalized = host.strip().lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _https_url(value: str | None, label: str) -> str:
    if not value:
        raise ValueError(f"{label} is required in OIDC mode")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"{label} must be a credential-free HTTPS URL")
    return value.rstrip("/")


def _https_origin(value: str | None, label: str) -> str:
    validated = _https_url(value, label)
    parsed = urlsplit(validated)
    if parsed.path not in {"", "/"}:
        raise ValueError(f"{label} must be an origin without a path")
    return validated


class WebDeploymentSettings(BaseSettings):
    """Environment-backed controls for exposing the browser application."""

    model_config = SettingsConfigDict(
        env_prefix="VMANAGE_WEB_",
        extra="ignore",
        case_sensitive=False,
    )

    auth_mode: AuthMode = "local"
    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1, le=65_535)
    state_dir: Path = Path("~/.vmanage-mcp/browser")
    public_url: str | None = None
    allowed_hosts: CsvTuple = ()
    oidc_issuer: str | None = None
    oidc_audience: str | None = None
    oidc_jwks_url: str | None = None
    oidc_ca_bundle: Path | None = None
    oidc_algorithms: CsvTuple = ("RS256", "ES256")
    oidc_jwks_cache_seconds: int = Field(default=3_600, ge=60, le=86_400)
    oidc_jwks_retry_seconds: int = Field(default=30, ge=1, le=300)
    oidc_http_timeout_seconds: float = Field(default=5.0, ge=1.0, le=30.0)
    oidc_clock_skew_seconds: int = Field(default=30, ge=0, le=300)
    tenant_id: str | None = Field(default=None, min_length=1, max_length=200)
    trusted_proxy_ips: CsvTuple = ("127.0.0.1", "::1")
    oidc_subject_claim: str = "sub"
    oidc_tenant_claim: str = "tid"
    oidc_name_claim: str = "name"
    oidc_roles_claim: str = "roles"
    oidc_token_use_claim: str | None = None
    oidc_required_token_use: str | None = None
    oidc_owner_roles: CsvTuple = ("vmanage-owner",)
    oidc_operator_roles: CsvTuple = ("vmanage-operator",)
    oidc_viewer_roles: CsvTuple = ("vmanage-viewer",)

    @field_validator(
        "allowed_hosts",
        "oidc_algorithms",
        "oidc_owner_roles",
        "oidc_operator_roles",
        "oidc_viewer_roles",
        "trusted_proxy_ips",
        mode="before",
    )
    @classmethod
    def parse_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(item.strip() for item in value.split(",") if item.strip())
        return value

    @field_validator("state_dir", "oidc_ca_bundle")
    @classmethod
    def expand_path(cls, value: Path | None) -> Path | None:
        return value.expanduser() if value is not None else None

    @model_validator(mode="after")
    def validate_security_boundary(self) -> WebDeploymentSettings:
        if self.auth_mode == "local":
            if not is_loopback_host(self.host):
                raise ValueError("local auth mode may bind only to a loopback host")
            return self

        self.public_url = _https_origin(self.public_url, "VMANAGE_WEB_PUBLIC_URL")
        self.oidc_issuer = _https_url(self.oidc_issuer, "VMANAGE_WEB_OIDC_ISSUER")
        self.oidc_jwks_url = _https_url(self.oidc_jwks_url, "VMANAGE_WEB_OIDC_JWKS_URL")
        if not self.oidc_audience:
            raise ValueError("VMANAGE_WEB_OIDC_AUDIENCE is required in OIDC mode")
        if not self.tenant_id:
            raise ValueError("VMANAGE_WEB_TENANT_ID is required in OIDC mode")
        if not self.allowed_hosts or "*" in self.allowed_hosts:
            raise ValueError("OIDC mode requires a finite VMANAGE_WEB_ALLOWED_HOSTS list")
        public_host = urlsplit(self.public_url).hostname
        if public_host not in self.allowed_hosts:
            raise ValueError("VMANAGE_WEB_ALLOWED_HOSTS must include the public URL host")
        if "*" in self.trusted_proxy_ips:
            raise ValueError("VMANAGE_WEB_TRUSTED_PROXY_IPS cannot trust every proxy")
        try:
            for network in self.trusted_proxy_ips:
                ipaddress.ip_network(network, strict=False)
        except ValueError as exc:
            raise ValueError(
                "VMANAGE_WEB_TRUSTED_PROXY_IPS must contain IP addresses or CIDR networks"
            ) from exc
        for host in self.allowed_hosts:
            if not re.fullmatch(r"(?:\*\.)?[A-Za-z0-9.-]+", host):
                raise ValueError("VMANAGE_WEB_ALLOWED_HOSTS contains an invalid hostname")
        configured_roles = {
            *self.oidc_owner_roles,
            *self.oidc_operator_roles,
            *self.oidc_viewer_roles,
        }
        if not configured_roles:
            raise ValueError("OIDC mode requires at least one configured application role")
        allowed_algorithms = {"RS256", "RS384", "RS512", "ES256", "ES384", "ES512", "EdDSA"}
        if not self.oidc_algorithms or not set(self.oidc_algorithms).issubset(allowed_algorithms):
            raise ValueError("VMANAGE_WEB_OIDC_ALGORITHMS must use supported asymmetric algorithms")
        if bool(self.oidc_token_use_claim) != bool(self.oidc_required_token_use):
            raise ValueError(
                "VMANAGE_WEB_OIDC_TOKEN_USE_CLAIM and REQUIRED_TOKEN_USE must be set together"
            )
        if self.oidc_ca_bundle is not None and not self.oidc_ca_bundle.is_file():
            raise ValueError("VMANAGE_WEB_OIDC_CA_BUNDLE must reference a readable file")
        return self

    @property
    def state_tenant_id(self) -> str:
        """Return the tenant identity that owns this deployment's local state."""
        return self.tenant_id if self.auth_mode == "oidc" and self.tenant_id else "local"


class DeploymentStateError(RuntimeError):
    """Raised when a state directory belongs to another deployment identity."""


def bind_state_directory(settings: WebDeploymentSettings) -> Path:
    """Create or verify the owner-only tenant binding for default browser state."""
    state_dir = settings.state_dir
    prepare_private_directory(state_dir)
    marker_path = state_dir / ".deployment.json"
    expected = {
        "schema_version": 1,
        "auth_mode": settings.auth_mode,
        "tenant_id": settings.state_tenant_id,
    }
    with _STATE_BINDING_LOCK:
        try:
            descriptor = os.open(marker_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            try:
                existing = json.loads(marker_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise DeploymentStateError("Deployment state binding is unreadable") from exc
            if existing != expected:
                raise DeploymentStateError(
                    "Deployment state belongs to another authentication mode or tenant"
                ) from None
        else:
            with os.fdopen(descriptor, "wb") as marker:
                marker.write(canonical_json_bytes(expected))
        if os.name != "nt":
            marker_path.chmod(0o600)
    return state_dir