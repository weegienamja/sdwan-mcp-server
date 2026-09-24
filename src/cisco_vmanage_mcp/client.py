"""Async HTTP client for Cisco vManage REST API.

Handles session-based authentication (JSESSIONID + XSRF token),
automatic re-authentication on session expiry, retry with exponential
backoff, per-request timeout handling, and audit logging.

Tested against DevNet sandbox-sdwan-2.cisco.com (v20.10.1).
"""

import asyncio
import logging
import os
import ssl
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx
from dotenv import dotenv_values, load_dotenv


def _load_configuration_environment(project_file: Path) -> Path:
    """Load external env > private setup file > project dotenv precedence."""
    external_keys = frozenset(os.environ)
    load_dotenv(project_file, override=False)
    config_file = Path(
        os.getenv(
            "VMANAGE_CONFIG_FILE",
            str(Path.home() / ".vmanage-mcp" / "vmanage.env"),
        )
    ).expanduser()
    if config_file.is_file():
        for name, value in dotenv_values(config_file).items():
            if name not in external_keys and isinstance(value, str):
                os.environ[name] = value
    return config_file


# Find configuration regardless of CWD while preserving explicit deployment env.
_project_root = Path(__file__).resolve().parent.parent.parent
_config_file = _load_configuration_environment(_project_root / ".env")

logger = logging.getLogger("cisco_vmanage_mcp.client")

# --- Exception taxonomy ---

class VManageError(Exception):
    """Base exception for all vManage client errors."""
    pass


class ConfigurationError(VManageError):
    """Raised when client environment configuration is invalid."""


class AuthenticationError(VManageError):
    """Raised when vManage authentication fails (bad credentials, expired token)."""
    pass


class VManageAPIError(VManageError):
    """Raised when a vManage API call returns an HTTP error."""
    def __init__(self, message: str, status_code: int | None = None, endpoint: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.endpoint = endpoint


class RateLimitError(VManageAPIError):
    """Raised when vManage returns HTTP 429 (rate limit exceeded)."""
    pass


class NotFoundError(VManageAPIError):
    """Raised when the requested resource or endpoint does not exist."""
    pass


class PermissionError(VManageAPIError):
    """Raised when the user role lacks required privileges."""
    pass


class ConnectionError(VManageError):
    """Raised when the client cannot connect to vManage."""
    pass


class TimeoutError(VManageError):
    """Raised when a request to vManage times out."""
    pass


# --- Retry configuration ---

_DEFAULT_MAX_RETRIES = 3
_DEFAULT_BACKOFF_BASE = 1.0  # seconds
_DEFAULT_BACKOFF_MAX = 10.0  # seconds
_MAX_RETRY_AFTER = 60.0  # seconds
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class VManageClient:
    """Async HTTP client for Cisco vManage REST API.

    Features:
    - Session-based authentication (JSESSIONID + XSRF token)
    - Automatic re-authentication on session expiry
    - Retry with exponential backoff for transient failures
    - Per-request timeout handling
    - Audit logging of every API call
    - Concurrency-safe: auth serialised via asyncio.Lock
    """

    @staticmethod
    def _parse_boolean(name: str, default: bool) -> bool:
        value = os.getenv(name)
        if value is None:
            return default
        normalized = value.strip().lower()
        if normalized == "true":
            return True
        if normalized == "false":
            return False
        raise ConfigurationError(f"{name} must be either 'true' or 'false'")

    @staticmethod
    def _parse_max_retries(raw_value: str | int | None = None) -> int:
        if raw_value is None:
            raw_value = os.getenv("VMANAGE_MAX_RETRIES", str(_DEFAULT_MAX_RETRIES))
        try:
            value = int(raw_value)
        except (TypeError, ValueError) as exc:
            raise ConfigurationError("VMANAGE_MAX_RETRIES must be an integer from 0 to 10") from exc
        if not 0 <= value <= 10:
            raise ConfigurationError("VMANAGE_MAX_RETRIES must be an integer from 0 to 10")
        return value

    def __init__(
        self,
        *,
        host: str | None = None,
        port: str | int | None = None,
        username: str | None = None,
        password: str | None = None,
        verify_ssl: bool | None = None,
        ca_bundle: str | None = None,
        max_retries: int | None = None,
    ) -> None:
        self.host = host if host is not None else os.getenv(
            "VMANAGE_HOST", "sandbox-sdwan-2.cisco.com"
        )
        configured_port = port if port is not None else os.getenv("VMANAGE_PORT", "443")
        self.port = str(configured_port)
        self.username = username if username is not None else os.getenv("VMANAGE_USERNAME")
        self.password = password if password is not None else os.getenv("VMANAGE_PASSWORD")
        self.verify_ssl = (
            verify_ssl
            if verify_ssl is not None
            else self._parse_boolean("VMANAGE_VERIFY_SSL", default=True)
        )
        self.tls_verify: bool | ssl.SSLContext = self.verify_ssl
        configured_ca_bundle = (
            ca_bundle if ca_bundle is not None else os.getenv("VMANAGE_CA_BUNDLE", "")
        ).strip()
        if configured_ca_bundle:
            if not self.verify_ssl:
                raise VManageError(
                    "VMANAGE_CA_BUNDLE cannot be used when VMANAGE_VERIFY_SSL=false"
                )
            try:
                self.tls_verify = ssl.create_default_context(
                    cafile=str(Path(configured_ca_bundle).expanduser())
                )
            except OSError as exc:
                raise VManageError(f"Unable to load VMANAGE_CA_BUNDLE: {exc}") from exc
        self.base_url = f"https://{self.host}:{self.port}"
        self._client: httpx.AsyncClient | None = None
        self._token: str | None = None
        self._auth_lock = asyncio.Lock()
        self.max_retries = self._parse_max_retries(max_retries)

        if not self.username or not self.password:
            raise AuthenticationError(
                "VMANAGE_USERNAME and VMANAGE_PASSWORD must be set in environment or .env file."
            )
        if not self.verify_ssl:
            logger.warning(
                "TLS certificate verification is disabled. "
                "Use VMANAGE_VERIFY_SSL=false only in an isolated lab."
            )

    async def _ensure_client(self) -> httpx.AsyncClient:
        """Create HTTP client if it doesn't exist."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                verify=self.tls_verify,
                timeout=60.0,
                follow_redirects=False,
            )
        return self._client

    async def _authenticate_locked(self) -> None:
        """Internal auth implementation -- caller must hold _auth_lock."""
        client = await self._ensure_client()

        try:
            login_response = await client.post(
                "/j_security_check",
                data={
                    "j_username": self.username,
                    "j_password": self.password,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except httpx.TimeoutException as exc:
            raise TimeoutError("Authentication request timed out.") from exc
        except httpx.ConnectError as exc:
            raise ConnectionError(
                f"Could not connect to vManage at {self.base_url}."
            ) from exc

        if login_response.status_code >= 400:
            raise AuthenticationError(
                f"vManage authentication failed (HTTP {login_response.status_code})."
            )

        if "<html" in login_response.text.lower():
            raise AuthenticationError(
                "vManage authentication failed. Check VMANAGE_USERNAME and VMANAGE_PASSWORD."
            )

        try:
            token_response = await client.get("/dataservice/client/token")
        except httpx.TimeoutException as exc:
            raise TimeoutError("XSRF token request timed out.") from exc

        if token_response.status_code == 200:
            token = token_response.text.strip()
            if not token:
                raise AuthenticationError("vManage returned an empty XSRF token.")
            self._token = token
            client.headers["X-XSRF-TOKEN"] = self._token
        else:
            raise AuthenticationError(
                f"Failed to obtain XSRF token from vManage (HTTP {token_response.status_code})."
            )

    async def authenticate(self) -> None:
        """Authenticate with vManage and obtain session + XSRF token.

        Serialised via asyncio.Lock so concurrent tool calls don't
        stomp on each other's sessions. If another coroutine already
        authenticated while we were waiting for the lock, we skip.
        """
        async with self._auth_lock:
            if self._token is not None:
                return
            await self._authenticate_locked()

    async def reauthenticate(self, stale_token: str | None = None) -> None:
        """Force re-authentication after a 302/401/403.

        Uses compare-and-swap: if another coroutine already refreshed
        the token while we waited for the lock, we skip re-auth.
        """
        async with self._auth_lock:
            if stale_token is not None and self._token != stale_token:
                # Another coroutine already refreshed the token
                return
            self._token = None
            await self._authenticate_locked()

    def _classify_error(self, status_code: int, endpoint: str) -> VManageAPIError:
        """Map HTTP status codes to specific exception types."""
        if status_code == 404:
            return NotFoundError(
                f"Resource not found: GET {endpoint}", status_code, endpoint
            )
        if status_code == 429:
            return RateLimitError(
                f"Rate limit exceeded on GET {endpoint}", status_code, endpoint
            )
        if status_code == 403:
            return PermissionError(
                f"Permission denied on GET {endpoint}", status_code, endpoint
            )
        return VManageAPIError(
            f"vManage API error: HTTP {status_code} on GET {endpoint}",
            status_code, endpoint,
        )

    @staticmethod
    def _retry_delay(response: httpx.Response, attempt: int) -> float:
        """Return exponential backoff, honoring a bounded Retry-After header."""
        default = min(
            _DEFAULT_BACKOFF_BASE * (2 ** attempt),
            _DEFAULT_BACKOFF_MAX,
        )
        retry_after = response.headers.get("Retry-After")
        if not retry_after:
            return default

        try:
            delay = float(retry_after)
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(retry_after)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=UTC)
                delay = (retry_at - datetime.now(UTC)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                return default

        return min(max(delay, 0.0), _MAX_RETRY_AFTER)

    async def _get_with_session_recovery(
        self,
        client: httpx.AsyncClient,
        endpoint: str,
        params: dict | None,
        stale_token: str | None,
    ) -> httpx.Response:
        response = await client.get(endpoint, params=params)
        if response.status_code in (302, 401, 403):
            await self.reauthenticate(stale_token=stale_token)
            return await client.get(endpoint, params=params)
        return response

    async def _prepare_response_retry(
        self,
        response: httpx.Response,
        endpoint: str,
        attempt: int,
        stale_token: str | None,
    ) -> VManageAPIError:
        if response.status_code == 503:
            try:
                await self.reauthenticate(stale_token=stale_token)
            except Exception as exc:
                logger.debug("Best-effort re-authentication failed: %s", exc)
        backoff = self._retry_delay(response, attempt)
        logger.warning(
            "Retryable error HTTP %d on GET %s (attempt %d/%d, backoff %.1fs)",
            response.status_code,
            endpoint,
            attempt + 1,
            self.max_retries + 1,
            backoff,
        )
        await asyncio.sleep(backoff)
        return self._classify_error(response.status_code, endpoint)

    async def _request_with_retry(
        self,
        endpoint: str,
        params: dict | None = None,
    ) -> httpx.Response:
        """Execute a GET request with retry, backoff, and audit logging."""
        from cisco_vmanage_mcp.services.audit import log_api_call

        client = await self._ensure_client()

        if self._token is None:
            await self.authenticate()

        last_error: Exception | None = None

        for attempt in range(self.max_retries + 1):
            token_at_request = self._token
            start = time.monotonic()
            try:
                response = await self._get_with_session_recovery(
                    client,
                    endpoint,
                    params,
                    token_at_request,
                )
                duration_ms = (time.monotonic() - start) * 1000
                log_api_call("GET", endpoint, response.status_code, duration_ms=duration_ms)

                # Success
                if response.status_code < 400:
                    return response

                # Retryable server errors
                if response.status_code in _RETRYABLE_STATUS_CODES and attempt < self.max_retries:
                    last_error = await self._prepare_response_retry(
                        response,
                        endpoint,
                        attempt,
                        token_at_request,
                    )
                    continue

                # Non-retryable error
                raise self._classify_error(response.status_code, endpoint)

            except httpx.TimeoutException as exc:
                duration_ms = (time.monotonic() - start) * 1000
                log_api_call("GET", endpoint, error="timeout", duration_ms=duration_ms)
                if attempt < self.max_retries:
                    backoff = min(_DEFAULT_BACKOFF_BASE * (2 ** attempt), _DEFAULT_BACKOFF_MAX)
                    logger.warning(
                        "Timeout on %s %s (attempt %d/%d, backoff %.1fs)",
                        "GET", endpoint, attempt + 1, self.max_retries + 1, backoff,
                    )
                    await asyncio.sleep(backoff)
                    last_error = TimeoutError(f"Request to {endpoint} timed out.")
                    continue
                raise TimeoutError(
                    f"Request to {endpoint} timed out after {self.max_retries + 1} attempts."
                ) from exc

            except httpx.ConnectError as exc:
                duration_ms = (time.monotonic() - start) * 1000
                log_api_call("GET", endpoint, error="connection_error", duration_ms=duration_ms)
                raise ConnectionError(
                    f"Could not connect to vManage at {self.base_url}."
                ) from exc

        # Exhausted retries
        if last_error:
            raise last_error
        raise VManageAPIError(f"Request to {endpoint} failed after {self.max_retries + 1} attempts.")

    async def get(self, endpoint: str, params: dict | None = None) -> dict:
        """Make authenticated GET request to vManage API.

        Features retry with exponential backoff for transient failures
        (429, 500, 502, 503, 504) and automatic re-auth on session expiry.

        Args:
            endpoint: API path (e.g., '/dataservice/device')
            params: Optional query parameters

        Returns:
            Parsed JSON response body

        Raises:
            VManageAPIError: On HTTP errors
            AuthenticationError: On auth failures
            TimeoutError: On request timeout
            ConnectionError: When vManage is unreachable
        """
        response = await self._request_with_retry(endpoint, params=params)
        try:
            return response.json()
        except ValueError as exc:
            raise VManageAPIError(
                f"vManage returned invalid JSON from GET {endpoint}",
                response.status_code,
                endpoint,
            ) from exc

    async def get_raw(self, endpoint: str, params: dict | None = None) -> str:
        """Make authenticated GET request returning raw text.

        Used for endpoints that return plain text (e.g., running config).

        Args:
            endpoint: API path
            params: Optional query parameters

        Returns:
            Raw response text
        """
        response = await self._request_with_retry(endpoint, params=params)
        return response.text

    async def close(self) -> None:
        """Close the HTTP client and invalidate the session."""
        if self._client:
            try:
                await self._client.get("/logout")
            except Exception as exc:
                logger.debug("vManage logout failed: %s", exc)
            await self._client.aclose()
            self._client = None
            self._token = None
