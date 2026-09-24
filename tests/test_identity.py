"""OIDC signature, claim, tenant, and role-mapping contracts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from cisco_vmanage_mcp.services.deployment import WebDeploymentSettings
from cisco_vmanage_mcp.services.identity import (
    IdentityError,
    IdentityUnavailableError,
    OidcIdentityVerifier,
)


def _settings() -> WebDeploymentSettings:
    return WebDeploymentSettings(
        auth_mode="oidc",
        host="0.0.0.0",
        public_url="https://operations.example.com",
        allowed_hosts=("operations.example.com",),
        oidc_issuer="https://identity.example.com/tenant",
        oidc_audience="cisco-operations",
        oidc_jwks_url="https://identity.example.com/tenant/keys",
        tenant_id="customer-a",
    )


def _key_material() -> tuple[rsa.RSAPrivateKey, dict]:
    private_key = rsa.generate_private_key(public_exponent=65_537, key_size=2_048)
    jwk = RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    jwk.update({"kid": "key-1", "use": "sig", "alg": "RS256"})
    return private_key, jwk


def _token(private_key: rsa.RSAPrivateKey, **updates) -> str:
    now = datetime.now(UTC)
    claims = {
        "iss": "https://identity.example.com/tenant",
        "aud": "cisco-operations",
        "sub": "user-123",
        "tid": "customer-a",
        "name": "Network Operator",
        "roles": ["vmanage-operator"],
        "iat": now,
        "exp": now + timedelta(minutes=5),
        **updates,
    }
    return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "key-1"})


@pytest.mark.asyncio
async def test_oidc_verifier_maps_signed_claims_and_caches_keys() -> None:
    private_key, jwk = _key_material()
    loads = 0

    async def load_jwks() -> dict:
        nonlocal loads
        loads += 1
        return {"keys": [jwk]}

    verifier = OidcIdentityVerifier(_settings(), jwks_loader=load_jwks)

    first = await verifier.verify(_token(private_key))
    second = await verifier.verify(_token(private_key))

    assert first == second
    assert first.id.startswith("oidc-")
    assert first.tenant_id == "customer-a"
    assert first.display_name == "Network Operator"
    assert first.role == "operator"
    assert loads == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"aud": "other-application"}, "invalid"),
        ({"tid": "customer-b"}, "tenant"),
        ({"roles": ["unrelated-role"]}, "role"),
        ({"exp": datetime.now(UTC) - timedelta(minutes=1)}, "invalid"),
    ],
)
async def test_oidc_verifier_rejects_untrusted_claims(updates, message) -> None:
    private_key, jwk = _key_material()

    async def load_jwks() -> dict:
        return {"keys": [jwk]}

    verifier = OidcIdentityVerifier(_settings(), jwks_loader=load_jwks)

    with pytest.raises(IdentityError, match=message):
        await verifier.verify(_token(private_key, **updates))


@pytest.mark.asyncio
async def test_oidc_verifier_rejects_unknown_signing_key() -> None:
    private_key, _jwk = _key_material()

    async def load_jwks() -> dict:
        return {"keys": []}

    verifier = OidcIdentityVerifier(_settings(), jwks_loader=load_jwks)

    with pytest.raises(IdentityError, match="signing keys"):
        await verifier.verify(_token(private_key))


@pytest.mark.asyncio
async def test_unknown_key_does_not_amplify_jwks_refreshes() -> None:
    private_key, _jwk = _key_material()
    _other_private_key, other_jwk = _key_material()
    other_jwk["kid"] = "key-2"
    loads = 0

    async def load_jwks() -> dict:
        nonlocal loads
        loads += 1
        return {"keys": [other_jwk]}

    verifier = OidcIdentityVerifier(_settings(), jwks_loader=load_jwks)

    for _ in range(2):
        with pytest.raises(IdentityError, match="unknown"):
            await verifier.verify(_token(private_key))

    assert loads == 1


@pytest.mark.asyncio
async def test_oidc_verifier_can_require_provider_token_use_claim() -> None:
    private_key, jwk = _key_material()
    settings = _settings().model_copy(
        update={"oidc_token_use_claim": "token_use", "oidc_required_token_use": "access"}
    )

    async def load_jwks() -> dict:
        return {"keys": [jwk]}

    verifier = OidcIdentityVerifier(settings, jwks_loader=load_jwks)

    assert (await verifier.verify(_token(private_key, token_use="access"))).role == "operator"
    with pytest.raises(IdentityError, match="type"):
        await verifier.verify(_token(private_key, token_use="id"))


@pytest.mark.asyncio
async def test_jwks_outage_is_backed_off_without_serving_stale_keys() -> None:
    private_key, _jwk = _key_material()
    loads = 0

    async def load_jwks() -> dict:
        nonlocal loads
        loads += 1
        raise IdentityUnavailableError("unavailable")

    verifier = OidcIdentityVerifier(_settings(), jwks_loader=load_jwks)

    for _ in range(2):
        with pytest.raises(IdentityUnavailableError):
            await verifier.verify(_token(private_key))

    assert loads == 1


@pytest.mark.asyncio
async def test_jwks_ignores_keys_not_authorized_for_verification() -> None:
    private_key, jwk = _key_material()
    jwk["use"] = "enc"

    async def load_jwks() -> dict:
        return {"keys": [jwk]}

    verifier = OidcIdentityVerifier(_settings(), jwks_loader=load_jwks)

    with pytest.raises(IdentityUnavailableError, match="no supported signing keys"):
        await verifier.verify(_token(private_key))