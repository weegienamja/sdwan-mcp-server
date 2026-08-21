"""Async HTTP client for Cisco vManage REST API.

Handles session-based authentication (JSESSIONID + XSRF token),
automatic re-authentication on session expiry, retry with exponential
backoff, per-request timeout handling, and audit logging.

Tested against DevNet sandbox-sdwan-2.cisco.com (v20.10.1).
"""

import asyncio
import httpx
import logging
import os
import time
from pathlib import Path
from dotenv import load_dotenv

# find .env at project root regardless of CWD (walks up from this file)
_project_root = Path(__file__).resolve().parent.parent.parent
load_dotenv(_project_root / ".env")

logger = logging.getLogger("cisco_vmanage_mcp.client")

# --- Exception taxonomy ---

class VManageError(Exception):
    """Base exception for all vManage client errors."""
    pass


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

    def __init__(self):
        self.host = os.getenv("VMANAGE_HOST", "sandbox-sdwan-2.cisco.com")
        self.port = os.getenv("VMANAGE_PORT", "443")
        self.username = os.getenv("VMANAGE_USERNAME")
        self.password = os.getenv("VMANAGE_PASSWORD")
        self.verify_ssl = os.getenv("VMANAGE_VERIFY_SSL", "false").lower() == "true"
        self.base_url = f"https://{self.host}:{self.port}"
        self._client: httpx.AsyncClient | None = None
        self._token: str | None = None
        self._auth_lock = asyncio.Lock()
        self.max_retries = int(os.getenv("VMANAGE_MAX_RETRIES", str(_DEFAULT_MAX_RETRIES)))

        if not self.username or not self.password:
            raise AuthenticationError(
                "VMANAGE_USERNAME and VMANAGE_PASSWORD must be set in environment or .env file."
            )

    async def _ensure_client(self) -> httpx.AsyncClient:
        """Create HTTP client if it doesn't exist."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                verify=self.verify_ssl,
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
        except httpx.TimeoutException:
            raise TimeoutError("Authentication request timed out.")
        except httpx.ConnectError:
            raise ConnectionError(
                f"Could not connect to vManage at {self.base_url}."
            )

        if "<html" in login_response.text.lower():
            raise AuthenticationError(
                "vManage authentication failed. Check VMANAGE_USERNAME and VMANAGE_PASSWORD."
            )

        try:
            token_response = await client.get("/dataservice/client/token")
        except httpx.TimeoutException:
            raise TimeoutError("XSRF token request timed out.")

        if token_response.status_code == 200:
            self._token = token_response.text.strip()
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

    async def _request_with_retry(
        self,
        method: str,
        endpoint: str,
        params: dict | None = None,
        json_data: dict | None = None,
        raw: bool = False,
    ) -> httpx.Response:
        """Execute an HTTP request with retry, backoff, and audit logging."""
        from cisco_vmanage_mcp.services.audit import log_api_call

        client = await self._ensure_client()

        if self._token is None:
            await self.authenticate()

        last_error: Exception | None = None

        for attempt in range(self.max_retries + 1):
            token_at_request = self._token
            start = time.monotonic()
            try:
                if method == "GET":
                    response = await client.get(endpoint, params=params)
                elif method == "POST":
                    response = await client.post(endpoint, json=json_data)
                else:
                    raise ValueError(f"Unsupported method: {method}")

                duration_ms = (time.monotonic() - start) * 1000

                # Re-auth on session expiry (vManage returns 403 for
                # expired XSRF tokens, not just real permission errors).
                # Uses compare-and-swap so only one coroutine re-auths.
                if response.status_code in (302, 401, 403):
                    await self.reauthenticate(stale_token=token_at_request)
                    if method == "GET":
                        response = await client.get(endpoint, params=params)
                    else:
                        response = await client.post(endpoint, json=json_data)
                    duration_ms = (time.monotonic() - start) * 1000

                log_api_call(method, endpoint, response.status_code, duration_ms=duration_ms)

                # Success
                if response.status_code < 400:
                    return response

                # Retryable server errors
                if response.status_code in _RETRYABLE_STATUS_CODES and attempt < self.max_retries:
                    # 503 often means session expired on DevNet sandbox;
                    # re-authenticate before retrying.
                    if response.status_code == 503:
                        try:
                            await self.reauthenticate(stale_token=token_at_request)
                        except Exception:
                            pass  # best-effort re-auth; retry may still work
                    backoff = min(
                        _DEFAULT_BACKOFF_BASE * (2 ** attempt),
                        _DEFAULT_BACKOFF_MAX,
                    )
                    logger.warning(
                        "Retryable error HTTP %d on %s %s (attempt %d/%d, backoff %.1fs)",
                        response.status_code, method, endpoint,
                        attempt + 1, self.max_retries + 1, backoff,
                    )
                    await asyncio.sleep(backoff)
                    last_error = self._classify_error(response.status_code, endpoint)
                    continue

                # Non-retryable error
                raise self._classify_error(response.status_code, endpoint)

            except httpx.TimeoutException:
                duration_ms = (time.monotonic() - start) * 1000
                log_api_call(method, endpoint, error="timeout", duration_ms=duration_ms)
                if attempt < self.max_retries:
                    backoff = min(_DEFAULT_BACKOFF_BASE * (2 ** attempt), _DEFAULT_BACKOFF_MAX)
                    logger.warning(
                        "Timeout on %s %s (attempt %d/%d, backoff %.1fs)",
                        method, endpoint, attempt + 1, self.max_retries + 1, backoff,
                    )
                    await asyncio.sleep(backoff)
                    last_error = TimeoutError(f"Request to {endpoint} timed out.")
                    continue
                raise TimeoutError(f"Request to {endpoint} timed out after {self.max_retries + 1} attempts.")

            except httpx.ConnectError:
                duration_ms = (time.monotonic() - start) * 1000
                log_api_call(method, endpoint, error="connection_error", duration_ms=duration_ms)
                raise ConnectionError(
                    f"Could not connect to vManage at {self.base_url}."
                )

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
        response = await self._request_with_retry("GET", endpoint, params=params)
        return response.json()

    async def get_raw(self, endpoint: str, params: dict | None = None) -> str:
        """Make authenticated GET request returning raw text.

        Used for endpoints that return plain text (e.g., running config).

        Args:
            endpoint: API path
            params: Optional query parameters

        Returns:
            Raw response text
        """
        response = await self._request_with_retry("GET", endpoint, params=params, raw=True)
        return response.text

    async def post(self, endpoint: str, json_data: dict | None = None) -> dict:
        """Make authenticated POST request to vManage API.

        Args:
            endpoint: API path
            json_data: Request body as dict

        Returns:
            Parsed JSON response body
        """
        response = await self._request_with_retry("POST", endpoint, json_data=json_data)
        return response.json()

    async def close(self) -> None:
        """Close the HTTP client and invalidate the session."""
        if self._client:
            try:
                await self._client.get("/logout")
            except Exception:
                pass
            await self._client.aclose()
            self._client = None
            self._token = None
