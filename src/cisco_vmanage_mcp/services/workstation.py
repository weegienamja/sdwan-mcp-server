"""A per-user Manager connection for local diagnostics."""

import ssl
from pathlib import Path

import httpx

# Exact read operations used by the evidence assistant. New operations require review.
_READ_PATHS = frozenset({
    "/dataservice/device",
    "/dataservice/alarms",
    "/dataservice/alarms/count",
    "/dataservice/device/bfd/sessions",
    "/dataservice/device/control/connections",
    "/dataservice/device/system/status",
})


def manager_origin(value: str) -> httpx.URL:
    """Accept a Manager HTTPS origin, optionally supplied as hostname:port."""
    value = value.strip()
    try:
        url = httpx.URL(value if "://" in value else "https://" + value)
        if (url.scheme == "https" and url.host and not url.userinfo
                and url.raw_path == b"/" and not url.fragment
                and not any(ch in value for ch in "@?#\\")
                and not any(ch.isspace() for ch in value)):
            return url
    except (ValueError, httpx.InvalidURL):
        pass
    raise ValueError("Enter your Manager HTTPS hostname and optional port, without a path.")


class ManagerReadError(RuntimeError):
    """A diagnostic request could not return usable Manager evidence."""


class ManagerReader:
    """GET-only API client. Manager validates the operator's personal token."""

    def __init__(
        self, origin: str, token: str, ca_file: Path | None = None,
        *, transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        url = manager_origin(origin)
        if not token or len(token) > 32768 or not token.isascii() or not token.isprintable() or " " in token:
            raise ValueError("Paste only the personal Manager API token.")
        tls = ssl.create_default_context(purpose=ssl.Purpose.SERVER_AUTH)
        if ca_file:
            tls.load_verify_locations(cafile=ca_file)
        self.sign_in_required = False
        self._client = httpx.AsyncClient(
            base_url=url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            verify=tls, trust_env=False, follow_redirects=False, timeout=30, transport=transport,
        )

    async def get(self, endpoint: str, params: dict | None = None) -> dict:
        if endpoint not in _READ_PATHS:
            raise ManagerReadError("This endpoint is outside the workstation's read-only operations.")
        try:
            response = await self._client.get(endpoint, params=params)
        except httpx.RequestError as exc:
            raise ManagerReadError("Cannot reach Manager. Check VPN, hostname and certificate trust.") from exc
        if response.status_code == 401 or response.is_redirect or (
            response.is_success and "text/html" in response.headers.get("content-type", "")
        ):
            self.sign_in_required = True
            raise ManagerReadError("Manager requires a new sign-in. Check your personal API token.")
        if response.status_code == 403:
            raise ManagerReadError(f"Your Cisco role cannot read {endpoint} (403).")
        if not response.is_success:
            raise ManagerReadError(f"Manager returned HTTP {response.status_code} for {endpoint}.")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ManagerReadError("Expected a JSON response from the Manager API.") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise ManagerReadError("Manager did not return an evidence table.")
        return payload

    async def close(self) -> None:
        await self._client.aclose()
        self._client.headers.pop("Authorization", None)
