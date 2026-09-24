"""HTTP security-boundary contracts for local and enterprise deployments."""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from cisco_vmanage_mcp.services.deployment import WebDeploymentSettings
from cisco_vmanage_mcp.services.governance import GovernanceActor
from cisco_vmanage_mcp.services.identity import IdentityError, IdentityUnavailableError
from cisco_vmanage_mcp.services.web_security import (
    BrowserAuditMiddleware,
    BrowserIdentityMiddleware,
    BrowserResponseSecurityMiddleware,
    request_actor,
)

LOCAL_ACTOR = GovernanceActor(
    id="local-owner",
    tenant_id="local",
    display_name="Local owner",
    role="owner",
)


class StubVerifier:
    def __init__(self, result) -> None:
        self.result = result

    async def verify(self, token: str) -> GovernanceActor:
        del token
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


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


def _application(settings: WebDeploymentSettings, verifier=None) -> Starlette:
    async def identity(request: Request) -> JSONResponse:
        return JSONResponse(request_actor(request).model_dump(mode="json"))

    async def live(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "live"})

    return Starlette(
        routes=[
            Route("/identity", identity, methods=["GET", "POST"]),
            Route("/health/live", live),
        ],
        middleware=[
            Middleware(
                BrowserResponseSecurityMiddleware,
                enterprise_mode=settings.auth_mode == "oidc",
            ),
            Middleware(BrowserAuditMiddleware),
            Middleware(
                BrowserIdentityMiddleware,
                settings=settings,
                local_actor=LOCAL_ACTOR,
                verifier=verifier,
            ),
        ],
    )


def test_local_mode_attaches_owner_without_token() -> None:
    with TestClient(_application(WebDeploymentSettings())) as browser:
        response = browser.get("/identity", headers={"X-Request-ID": "request-123"})

    assert response.json()["id"] == "local-owner"
    assert response.headers["x-request-id"] == "request-123"
    assert "fonts.googleapis.com" not in response.headers["content-security-policy"]
    assert "font-src 'self'" in response.headers["content-security-policy"]


def test_local_mode_rejects_non_loopback_clients_even_under_external_asgi_server() -> None:
    application = _application(WebDeploymentSettings())

    with TestClient(
        application,
        base_url="http://127.0.0.1",
        client=("203.0.113.10", 50_000),
    ) as browser:
        response = browser.get("/identity")

    assert response.status_code == 403


def test_enterprise_mode_requires_https_bearer_token() -> None:
    actor = LOCAL_ACTOR.model_copy(update={"tenant_id": "customer-a"})
    application = _application(_settings(), StubVerifier(actor))

    with TestClient(application, base_url="http://operations.example.com") as browser:
        insecure = browser.get("/identity", headers={"Authorization": "Bearer token"})
    with TestClient(application, base_url="https://operations.example.com") as browser:
        missing = browser.get("/identity")

    assert insecure.status_code == 400
    assert missing.status_code == 401
    assert missing.headers["www-authenticate"].startswith("Bearer")


def test_enterprise_mode_enforces_verified_actor_and_viewer_read_only() -> None:
    viewer = GovernanceActor(
        id="oidc-viewer",
        tenant_id="customer-a",
        display_name="Viewer",
        role="viewer",
    )
    application = _application(_settings(), StubVerifier(viewer))
    headers = {"Authorization": "Bearer signed-token"}

    with TestClient(application, base_url="https://operations.example.com") as browser:
        allowed = browser.get("/identity", headers=headers)
        blocked = browser.post("/identity", headers=headers)
        cross_site = browser.post(
            "/identity",
            headers={**headers, "Origin": "https://attacker.example"},
        )

    assert allowed.json()["role"] == "viewer"
    assert blocked.status_code == 403
    assert cross_site.status_code == 403
    assert allowed.headers["strict-transport-security"].startswith("max-age=")


def test_identity_failures_are_generic_and_health_is_public() -> None:
    settings = _settings()
    invalid = _application(settings, StubVerifier(IdentityError("details")))
    unavailable = _application(settings, StubVerifier(IdentityUnavailableError("details")))
    headers = {"Authorization": "Bearer token"}

    with TestClient(invalid, base_url="https://operations.example.com") as browser:
        rejected = browser.get("/identity", headers=headers)
        live = browser.get("/health/live")
    with TestClient(unavailable, base_url="https://operations.example.com") as browser:
        failed_dependency = browser.get("/identity", headers=headers)

    assert rejected.status_code == 401
    assert "details" not in rejected.text
    assert failed_dependency.status_code == 503
    assert live.status_code == 200


def test_enterprise_mode_trusts_forwarded_https_only_from_configured_proxy() -> None:
    actor = LOCAL_ACTOR.model_copy(update={"tenant_id": "customer-a"})
    application = _application(_settings(), StubVerifier(actor))
    headers = {
        "Authorization": "Bearer token",
        "X-Forwarded-Proto": "https",
    }

    with TestClient(
        application,
        base_url="http://operations.example.com",
        client=("127.0.0.1", 50_000),
    ) as browser:
        trusted = browser.get("/identity", headers=headers)
    with TestClient(
        application,
        base_url="http://operations.example.com",
        client=("203.0.113.10", 50_000),
    ) as browser:
        untrusted = browser.get("/identity", headers=headers)
        probe = browser.get("/health/live", headers={"Host": "10.0.0.5:8765"})

    assert trusted.status_code == 200
    assert trusted.headers["strict-transport-security"].startswith("max-age=")
    assert untrusted.status_code == 400
    assert probe.status_code == 200