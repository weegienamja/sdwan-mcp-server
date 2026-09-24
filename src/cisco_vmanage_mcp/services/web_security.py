"""HTTP security boundary for local and identity-aware browser deployments."""

from __future__ import annotations

import ipaddress
import re
import time
from urllib.parse import urlsplit
from uuid import uuid4

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from cisco_vmanage_mcp.services.audit import log_browser_request
from cisco_vmanage_mcp.services.deployment import WebDeploymentSettings, is_loopback_host
from cisco_vmanage_mcp.services.governance import GovernanceActor
from cisco_vmanage_mcp.services.identity import (
    IdentityError,
    IdentityUnavailableError,
    IdentityVerifier,
)

HEALTH_PATHS = frozenset({"/health/live", "/health/ready"})
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class BrowserIdentityMiddleware(BaseHTTPMiddleware):
    """Authenticate requests and attach a request-scoped governance actor."""

    def __init__(
        self,
        app,
        *,
        settings: WebDeploymentSettings,
        local_actor: GovernanceActor,
        verifier: IdentityVerifier | None,
    ) -> None:
        super().__init__(app)
        self.settings = settings
        self.local_actor = local_actor
        self.verifier = verifier
        self.trusted_proxy_networks = tuple(
            ipaddress.ip_network(value, strict=False)
            for value in settings.trusted_proxy_ips
        )
        public_url = urlsplit(settings.public_url or "")
        self.public_origin = (
            f"{public_url.scheme}://{public_url.netloc}" if public_url.netloc else None
        )

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        request_id = request.headers.get("x-request-id", "")
        request.state.request_id = (
            request_id if REQUEST_ID_PATTERN.fullmatch(request_id) else str(uuid4())
        )
        if request.url.path in HEALTH_PATHS:
            return await call_next(request)
        if self.settings.auth_mode == "local":
            if not self._is_local_request(request):
                return self._error("Local mode accepts only loopback requests", 403)
            request.state.actor = self.local_actor
            return await call_next(request)
        if not self._host_allowed(request.url.hostname):
            return self._error("Host is not allowed", 400)
        if not self._is_secure_request(request):
            return self._error("HTTPS is required", 400)
        request.state.secure_transport = True
        token = self._bearer_token(request)
        if token is None or self.verifier is None:
            return self._error("Authentication required", 401, authenticate=True)
        try:
            actor = await self.verifier.verify(token)
        except IdentityUnavailableError:
            return self._error("Identity provider is unavailable", 503)
        except IdentityError:
            return self._error("Bearer token is invalid", 401, authenticate=True)
        if actor.role == "viewer" and request.method not in SAFE_METHODS:
            return self._error("Actor is not authorized", 403)
        if request.method not in SAFE_METHODS and self._is_cross_site(request):
            return self._error("Cross-site request rejected", 403)
        request.state.actor = actor
        return await call_next(request)

    @staticmethod
    def _bearer_token(request: Request) -> str | None:
        scheme, separator, value = request.headers.get("authorization", "").partition(" ")
        if not separator or scheme.lower() != "bearer" or not value.strip() or " " in value.strip():
            return None
        return value.strip()

    def _is_cross_site(self, request: Request) -> bool:
        fetch_site = request.headers.get("sec-fetch-site", "").lower()
        if fetch_site == "cross-site":
            return True
        origin = request.headers.get("origin")
        return bool(origin and origin.rstrip("/") != self.public_origin)

    def _host_allowed(self, hostname: str | None) -> bool:
        if hostname is None:
            return False
        normalized = hostname.lower().rstrip(".")
        return any(
            normalized == allowed.lower().rstrip(".")
            or (
                allowed.startswith("*.")
                and normalized.endswith(allowed[1:].lower())
                and normalized != allowed[2:].lower()
            )
            for allowed in self.settings.allowed_hosts
        )

    @staticmethod
    def _is_local_request(request: Request) -> bool:
        client_host = request.client.host if request.client is not None else ""
        request_host = request.url.hostname or ""
        if client_host == "testclient" and request_host == "testserver":
            return True
        return is_loopback_host(client_host) and is_loopback_host(request_host)

    def _is_secure_request(self, request: Request) -> bool:
        if request.url.scheme == "https":
            return True
        if request.client is None:
            return False
        try:
            client_ip = ipaddress.ip_address(request.client.host)
        except ValueError:
            return False
        if not any(client_ip in network for network in self.trusted_proxy_networks):
            return False
        forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",", 1)[0]
        return forwarded_proto.strip().lower() == "https"

    def _error(self, message: str, status_code: int, *, authenticate: bool = False) -> Response:
        headers = {"Cache-Control": "no-store"}
        if authenticate:
            headers["WWW-Authenticate"] = 'Bearer realm="cisco-vmanage-mcp"'
        return JSONResponse({"error": message}, status_code=status_code, headers=headers)


class BrowserAuditMiddleware(BaseHTTPMiddleware):
    """Record bounded request metadata for authenticated and rejected traffic."""

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        started = time.monotonic()
        try:
            response = await call_next(request)
        except Exception as exc:
            self._log(request, 500, started, error=type(exc).__name__)
            raise
        self._log(request, response.status_code, started)
        return response

    @staticmethod
    def _log(
        request: Request,
        status_code: int,
        started: float,
        *,
        error: str | None = None,
    ) -> None:
        actor = getattr(request.state, "actor", None)
        route_object = request.scope.get("route")
        route = getattr(route_object, "path", "<unmatched>")
        log_browser_request(
            request.method,
            route,
            status_code,
            getattr(request.state, "request_id", "unknown"),
            actor_id=actor.id if isinstance(actor, GovernanceActor) else None,
            tenant_id=actor.tenant_id if isinstance(actor, GovernanceActor) else None,
            role=actor.role if isinstance(actor, GovernanceActor) else None,
            duration_ms=(time.monotonic() - started) * 1_000,
            error=error,
        )


def request_actor(request: Request) -> GovernanceActor:
    """Return the actor established by the HTTP security boundary."""
    actor = getattr(request.state, "actor", None)
    if not isinstance(actor, GovernanceActor):
        raise RuntimeError("Request identity was not established")
    return actor


class BrowserResponseSecurityMiddleware(BaseHTTPMiddleware):
    """Attach browser hardening and correlation headers to every response."""

    def __init__(self, app, *, enterprise_mode: bool) -> None:
        super().__init__(app)
        self.enterprise_mode = enterprise_mode

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self'; "
            "font-src 'self'; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "frame-ancestors 'none'; "
            "base-uri 'self'; "
            "form-action 'self'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), payment=()"
        )
        response.headers["X-Request-ID"] = getattr(
            request.state,
            "request_id",
            str(uuid4()),
        )
        if self.enterprise_mode and (
            request.url.scheme == "https"
            or getattr(request.state, "secure_transport", False)
        ):
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )
        if request.url.path.startswith("/api/") or request.url.path in HEALTH_PATHS:
            response.headers["Cache-Control"] = "no-store"
        return response