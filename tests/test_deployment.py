"""Contracts for fail-closed local and enterprise deployment settings."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cisco_vmanage_mcp.services.deployment import (
    DeploymentStateError,
    WebDeploymentSettings,
    bind_state_directory,
)


def test_local_mode_defaults_to_loopback(monkeypatch) -> None:
    monkeypatch.delenv("VMANAGE_WEB_AUTH_MODE", raising=False)
    monkeypatch.delenv("VMANAGE_WEB_HOST", raising=False)

    settings = WebDeploymentSettings()

    assert settings.auth_mode == "local"
    assert settings.host == "127.0.0.1"


def test_local_mode_rejects_remote_bind(monkeypatch) -> None:
    monkeypatch.setenv("VMANAGE_WEB_AUTH_MODE", "local")
    monkeypatch.setenv("VMANAGE_WEB_HOST", "0.0.0.0")

    with pytest.raises(ValidationError, match="loopback"):
        WebDeploymentSettings()


def test_oidc_mode_requires_complete_https_configuration(monkeypatch) -> None:
    monkeypatch.setenv("VMANAGE_WEB_AUTH_MODE", "oidc")
    monkeypatch.setenv("VMANAGE_WEB_HOST", "0.0.0.0")

    with pytest.raises(ValidationError, match="PUBLIC_URL"):
        WebDeploymentSettings()


def test_oidc_mode_accepts_provider_neutral_claim_mapping(monkeypatch) -> None:
    monkeypatch.setenv("VMANAGE_WEB_AUTH_MODE", "oidc")
    monkeypatch.setenv("VMANAGE_WEB_HOST", "0.0.0.0")
    monkeypatch.setenv("VMANAGE_WEB_PUBLIC_URL", "https://operations.example.com")
    monkeypatch.setenv("VMANAGE_WEB_ALLOWED_HOSTS", "operations.example.com")
    monkeypatch.setenv("VMANAGE_WEB_OIDC_ISSUER", "https://identity.example.com/tenant")
    monkeypatch.setenv("VMANAGE_WEB_OIDC_AUDIENCE", "cisco-operations")
    monkeypatch.setenv("VMANAGE_WEB_TENANT_ID", "customer-a")
    monkeypatch.setenv(
        "VMANAGE_WEB_OIDC_JWKS_URL",
        "https://identity.example.com/tenant/discovery/keys",
    )
    monkeypatch.setenv("VMANAGE_WEB_OIDC_TENANT_CLAIM", "organization_id")
    monkeypatch.setenv("VMANAGE_WEB_OIDC_OWNER_ROLES", "network-admin,platform-admin")

    settings = WebDeploymentSettings()

    assert settings.host == "0.0.0.0"
    assert settings.oidc_tenant_claim == "organization_id"
    assert settings.oidc_owner_roles == ("network-admin", "platform-admin")


def test_oidc_mode_rejects_unbounded_proxy_trust_and_public_url_paths(monkeypatch) -> None:
    base = {
        "auth_mode": "oidc",
        "host": "0.0.0.0",
        "public_url": "https://operations.example.com",
        "allowed_hosts": ("operations.example.com",),
        "oidc_issuer": "https://identity.example.com/tenant",
        "oidc_audience": "cisco-operations",
        "oidc_jwks_url": "https://identity.example.com/tenant/keys",
        "tenant_id": "customer-a",
    }

    with pytest.raises(ValidationError, match="cannot trust every proxy"):
        WebDeploymentSettings(**base, trusted_proxy_ips=("*",))
    with pytest.raises(ValidationError, match="without a path"):
        WebDeploymentSettings(
            **{**base, "public_url": "https://operations.example.com/app"}
        )
    with pytest.raises(ValidationError, match="set together"):
        WebDeploymentSettings(**base, oidc_token_use_claim="token_use")


def test_state_directory_is_bound_to_one_auth_mode_and_tenant(tmp_path) -> None:
    state_dir = tmp_path / "state"
    local = WebDeploymentSettings(state_dir=state_dir)

    assert bind_state_directory(local) == state_dir
    assert bind_state_directory(local) == state_dir
    assert (state_dir / ".deployment.json").stat().st_mode & 0o777 == 0o600

    oidc = WebDeploymentSettings(
        auth_mode="oidc",
        host="0.0.0.0",
        state_dir=state_dir,
        public_url="https://operations.example.com",
        allowed_hosts=("operations.example.com",),
        oidc_issuer="https://identity.example.com/tenant",
        oidc_audience="cisco-operations",
        oidc_jwks_url="https://identity.example.com/tenant/keys",
        tenant_id="customer-a",
    )
    with pytest.raises(DeploymentStateError, match="another"):
        bind_state_directory(oidc)