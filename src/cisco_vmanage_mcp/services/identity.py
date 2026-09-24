"""Provider-neutral OIDC identity verification for enterprise deployments."""

from __future__ import annotations

import asyncio
import hashlib
import json
import ssl
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol, cast

import httpx
import jwt

from cisco_vmanage_mcp.services.deployment import WebDeploymentSettings
from cisco_vmanage_mcp.services.governance import ActorRole, GovernanceActor

JwksLoader = Callable[[], Awaitable[dict[str, Any]]]
MAX_BEARER_TOKEN_LENGTH = 16_384
MAX_JWKS_DOCUMENT_BYTES = 524_288
UNKNOWN_KEY_REFRESH_SECONDS = 60


class IdentityError(RuntimeError):
    """Raised when an external identity cannot be authenticated or authorized."""


class IdentityUnavailableError(IdentityError):
    """Raised when identity-provider verification material is unavailable."""


class IdentityVerifier(Protocol):
    """Resolve a verified bearer token to an application actor."""

    async def verify(self, token: str) -> GovernanceActor: ...


def _claim(claims: Mapping[str, Any], path: str) -> Any:
    value: Any = claims
    for segment in path.split("."):
        if not isinstance(value, Mapping) or segment not in value:
            return None
        value = value[segment]
    return value


def _claim_values(value: Any) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, (list, tuple, set)):
        return {item for item in value if isinstance(item, str)}
    return set()


class OidcIdentityVerifier:
    """Verify signed OIDC JWTs and map claims to tenant-scoped human actors."""

    def __init__(
        self,
        settings: WebDeploymentSettings,
        *,
        jwks_loader: JwksLoader | None = None,
    ) -> None:
        if settings.auth_mode != "oidc":
            raise ValueError("OIDC identity verification requires oidc auth mode")
        self.settings = settings
        self._jwks_loader = jwks_loader or self._fetch_jwks
        self._keys: dict[str, jwt.PyJWK] = {}
        self._keys_expires_at = 0.0
        self._next_unknown_key_refresh_at = 0.0
        self._next_refresh_attempt_at = 0.0
        self._refresh_lock = asyncio.Lock()

    async def verify(self, token: str) -> GovernanceActor:
        """Validate one access token and return its bounded application identity."""
        try:
            if len(token) > MAX_BEARER_TOKEN_LENGTH:
                raise IdentityError("Bearer token is invalid")
            header = jwt.get_unverified_header(token)
            algorithm = header.get("alg")
            key_id = header.get("kid")
            if algorithm not in self.settings.oidc_algorithms or not isinstance(key_id, str):
                raise IdentityError("Token signing metadata is not allowed")
            key = await self._signing_key(key_id)
            claims = jwt.decode(
                token,
                key=key,
                algorithms=[algorithm],
                audience=self.settings.oidc_audience,
                issuer=self.settings.oidc_issuer,
                leeway=self.settings.oidc_clock_skew_seconds,
                options={"require": ["exp", "iat", "iss", "sub", "aud"]},
            )
            return self._actor_from_claims(claims)
        except IdentityError:
            raise
        except (jwt.PyJWTError, KeyError, TypeError, ValueError) as exc:
            raise IdentityError("Bearer token is invalid") from exc

    async def _signing_key(self, key_id: str) -> Any:
        now = time.monotonic()
        if now < self._keys_expires_at and key_id in self._keys:
            return self._keys[key_id]
        async with self._refresh_lock:
            now = time.monotonic()
            if now >= self._keys_expires_at:
                await self._refresh_keys_with_backoff(now)
            elif key_id not in self._keys and now >= self._next_unknown_key_refresh_at:
                await self._refresh_keys_with_backoff(now)
            if key_id in self._keys:
                return self._keys[key_id]
        raise IdentityError("Bearer token signing key is unknown")

    async def _refresh_keys_with_backoff(self, now: float) -> None:
        if now < self._next_refresh_attempt_at:
            raise IdentityUnavailableError("OIDC signing keys are temporarily unavailable")
        try:
            await self._refresh_keys()
        except IdentityUnavailableError:
            self._next_refresh_attempt_at = now + self.settings.oidc_jwks_retry_seconds
            raise

    async def _refresh_keys(self) -> None:
        document = await self._jwks_loader()
        keys = document.get("keys")
        if not isinstance(keys, list):
            raise IdentityUnavailableError("OIDC key set is invalid")
        parsed: dict[str, jwt.PyJWK] = {}
        for item in keys:
            if not isinstance(item, dict) or not isinstance(item.get("kid"), str):
                continue
            if item.get("use", "sig") != "sig":
                continue
            key_operations = item.get("key_ops", ["verify"])
            if not isinstance(key_operations, list) or "verify" not in key_operations:
                continue
            if item.get("alg") not in {None, *self.settings.oidc_algorithms}:
                continue
            try:
                parsed[item["kid"]] = jwt.PyJWK.from_dict(item)
            except (jwt.PyJWTError, KeyError, TypeError, ValueError):
                continue
        if not parsed:
            raise IdentityUnavailableError("OIDC key set contains no supported signing keys")
        self._keys = parsed
        refreshed_at = time.monotonic()
        self._next_refresh_attempt_at = 0.0
        self._keys_expires_at = refreshed_at + self.settings.oidc_jwks_cache_seconds
        self._next_unknown_key_refresh_at = refreshed_at + UNKNOWN_KEY_REFRESH_SECONDS

    async def _fetch_jwks(self) -> dict[str, Any]:
        verify: bool | ssl.SSLContext = True
        if self.settings.oidc_ca_bundle is not None:
            verify = ssl.create_default_context(cafile=str(self.settings.oidc_ca_bundle))
        try:
            async with httpx.AsyncClient(
                verify=verify,
                timeout=self.settings.oidc_http_timeout_seconds,
                follow_redirects=False,
            ) as client:
                async with client.stream(
                    "GET",
                    cast(str, self.settings.oidc_jwks_url),
                ) as response:
                    response.raise_for_status()
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > MAX_JWKS_DOCUMENT_BYTES:
                            raise IdentityUnavailableError("OIDC key set is too large")
                payload = json.loads(content)
        except IdentityUnavailableError:
            raise
        except (httpx.HTTPError, ValueError, UnicodeDecodeError) as exc:
            raise IdentityUnavailableError("OIDC signing keys are unavailable") from exc
        if not isinstance(payload, dict):
            raise IdentityUnavailableError("OIDC key set is invalid")
        return cast(dict[str, Any], payload)

    def _actor_from_claims(self, claims: Mapping[str, Any]) -> GovernanceActor:
        subject = _claim(claims, self.settings.oidc_subject_claim)
        tenant = _claim(claims, self.settings.oidc_tenant_claim)
        display_name = _claim(claims, self.settings.oidc_name_claim)
        roles = _claim_values(_claim(claims, self.settings.oidc_roles_claim))
        if not isinstance(subject, str) or not subject.strip():
            raise IdentityError("Bearer token subject is missing")
        if tenant != self.settings.tenant_id:
            raise IdentityError("Bearer token tenant is not authorized")
        if self.settings.oidc_token_use_claim is not None:
            token_use = _claim(claims, self.settings.oidc_token_use_claim)
            if token_use != self.settings.oidc_required_token_use:
                raise IdentityError("Bearer token type is not authorized")
        role = self._application_role(roles)
        stable_identity = "\x1f".join(
            (cast(str, self.settings.oidc_issuer), cast(str, tenant), subject)
        )
        actor_id = f"oidc-{hashlib.sha256(stable_identity.encode()).hexdigest()[:32]}"
        return GovernanceActor(
            id=actor_id,
            tenant_id=cast(str, tenant),
            display_name=(
                display_name.strip()[:200]
                if isinstance(display_name, str) and display_name.strip()
                else subject.strip()[:200]
            ),
            role=role,
        )

    def _application_role(self, roles: set[str]) -> ActorRole:
        if roles.intersection(self.settings.oidc_owner_roles):
            return "owner"
        if roles.intersection(self.settings.oidc_operator_roles):
            return "operator"
        if roles.intersection(self.settings.oidc_viewer_roles):
            return "viewer"
        raise IdentityError("Bearer token has no authorized application role")