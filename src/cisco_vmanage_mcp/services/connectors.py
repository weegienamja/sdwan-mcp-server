"""Isolated external connector registry with provenance and timeout controls."""

from __future__ import annotations

import asyncio
import json
import os
import ssl
import time
from collections.abc import Sequence
from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol, cast
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field

from cisco_vmanage_mcp.services.investigations import sanitize_investigation_data

ConnectorState = Literal["available", "unconfigured", "degraded"]
CollectionState = Literal["ok", "failed", "disabled"]
DataClassification = Literal["operational", "restricted", "sensitive"]

_ALLOWED_CONTEXT_KEYS = frozenset({
    "agent_id",
    "alert_id",
    "site_id",
    "system_ip",
    "test_id",
    "window_hours",
})


class ConnectorDescriptor(BaseModel):
    """Public metadata for one connector, excluding all credentials."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,63}$")
    display_name: str = Field(min_length=1, max_length=100)
    kind: str = Field(min_length=1, max_length=50)
    enabled: bool
    state: ConnectorState = "available"
    timeout_ms: int = Field(ge=10, le=120_000)
    data_classification: DataClassification
    capabilities: tuple[str, ...]
    provenance: str
    handoff_url: str | None = None


class ConnectorEvidence(BaseModel):
    """Normalized evidence returned by a connector."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    connector_id: str
    capability: str
    title: str
    severity: str = "unknown"
    data: dict[str, Any] = Field(default_factory=dict)
    provenance: str
    collected_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ConnectorSourceResult(BaseModel):
    """Outcome metadata for one connector collection attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    connector_id: str
    state: CollectionState
    provenance: str
    duration_ms: float = 0
    item_count: int = 0
    error: str | None = None


class ConnectorCollection(BaseModel):
    """Combined connector evidence that remains useful after partial failure."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    capability: str
    partial: bool
    items: tuple[ConnectorEvidence, ...]
    sources: tuple[ConnectorSourceResult, ...]


class Connector(Protocol):
    """Common connector behavior registered by the platform."""

    descriptor: ConnectorDescriptor

    async def collect(
        self,
        capability: str,
        context: dict[str, Any],
    ) -> Sequence[ConnectorEvidence]: ...


class MCPToolCaller(Protocol):
    """Minimal transport used by an MCP-backed connector."""

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]: ...


class ConnectorRegistry:
    """Register connectors and collect from them with fault isolation."""

    def __init__(self) -> None:
        self._connectors: dict[str, Connector] = {}

    def register(self, connector: Connector) -> None:
        """Register one unique connector."""
        connector_id = connector.descriptor.id
        if connector_id in self._connectors:
            raise ValueError(f"Connector already registered: {connector_id}")
        self._connectors[connector_id] = connector

    def describe(self) -> list[ConnectorDescriptor]:
        """Return public connector metadata in stable identifier order."""
        return [self._connectors[key].descriptor for key in sorted(self._connectors)]

    async def collect(
        self,
        capability: str,
        context: dict[str, Any] | None = None,
        *,
        connector_ids: set[str] | None = None,
    ) -> ConnectorCollection:
        """Collect one capability without allowing a connector failure to escape."""
        selected = [
            connector
            for connector in self._connectors.values()
            if capability in connector.descriptor.capabilities
            and (connector_ids is None or connector.descriptor.id in connector_ids)
        ]
        safe_context = cast(
            dict[str, Any],
            sanitize_investigation_data(context or {}),
        )

        async def collect_one(
            connector: Connector,
        ) -> tuple[list[ConnectorEvidence], ConnectorSourceResult]:
            descriptor = connector.descriptor
            if not descriptor.enabled:
                return [], ConnectorSourceResult(
                    connector_id=descriptor.id,
                    state="disabled",
                    provenance=descriptor.provenance,
                )
            started = time.monotonic()
            try:
                async with asyncio.timeout(descriptor.timeout_ms / 1_000):
                    returned = await connector.collect(capability, safe_context)
                items = [
                    item.model_copy(
                        update={
                            "connector_id": descriptor.id,
                            "capability": capability,
                            "provenance": descriptor.provenance,
                            "data": sanitize_investigation_data(item.data),
                        }
                    )
                    for item in returned[:100]
                ]
                return items, ConnectorSourceResult(
                    connector_id=descriptor.id,
                    state="ok",
                    provenance=descriptor.provenance,
                    duration_ms=round((time.monotonic() - started) * 1_000, 1),
                    item_count=len(items),
                )
            except TimeoutError:
                error = "timeout"
            except Exception as exc:
                error = type(exc).__name__
            return [], ConnectorSourceResult(
                connector_id=descriptor.id,
                state="failed",
                provenance=descriptor.provenance,
                duration_ms=round((time.monotonic() - started) * 1_000, 1),
                error=error,
            )

        results = await asyncio.gather(*(collect_one(connector) for connector in selected))
        items = sorted(
            (item for connector_items, _source in results for item in connector_items),
            key=lambda item: (item.connector_id, item.id),
        )
        sources = sorted((source for _items, source in results), key=lambda source: source.connector_id)
        return ConnectorCollection(
            capability=capability,
            partial=any(source.state == "failed" for source in sources),
            items=tuple(items),
            sources=tuple(sources),
        )


class PassiveVManageConnector:
    """Describe the core vManage evidence source inside the common registry."""

    descriptor = ConnectorDescriptor(
        id="vmanage",
        display_name="Cisco Catalyst SD-WAN Manager",
        kind="network-controller",
        enabled=True,
        timeout_ms=30_000,
        data_classification="restricted",
        capabilities=("inventory", "topology", "alarms", "assurance"),
        provenance="vmanage://local-client",
    )

    async def collect(
        self,
        capability: str,
        context: dict[str, Any],
    ) -> Sequence[ConnectorEvidence]:
        return ()


class StreamableHTTPMCPToolCaller:
    """Call tools on an HTTPS MCP server using the installed official SDK."""

    def __init__(
        self,
        url: str,
        token: str,
        *,
        timeout_seconds: float = 30,
        ca_bundle: str | None = None,
    ) -> None:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("Remote MCP connector URL must use HTTPS")
        if not token:
            raise ValueError("Remote MCP connector token is required")
        self.url = url
        self._token = token
        self.timeout_seconds = min(max(timeout_seconds, 1), 120)
        self._verify: bool | ssl.SSLContext = True
        if ca_bundle:
            self._verify = ssl.create_default_context(cafile=str(Path(ca_bundle).expanduser()))

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Open a bounded MCP session, call one tool, and normalize its result."""
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        headers = {"Authorization": f"Bearer {self._token}"}
        async with AsyncExitStack() as stack:
            http_client = await stack.enter_async_context(
                httpx.AsyncClient(
                    headers=headers,
                    timeout=self.timeout_seconds,
                    verify=self._verify,
                )
            )
            read_stream, write_stream, _session_id = await stack.enter_async_context(
                streamable_http_client(self.url, http_client=http_client)
            )
            session = await stack.enter_async_context(
                ClientSession(
                    read_stream,
                    write_stream,
                    read_timeout_seconds=timedelta(seconds=self.timeout_seconds),
                )
            )
            await session.initialize()
            result = await session.call_tool(name, arguments=arguments)
        payload = result.model_dump(mode="json", by_alias=True)
        if payload.get("isError"):
            raise RuntimeError("Remote MCP tool returned an error")
        structured = payload.get("structuredContent") or payload.get("structured_content")
        if isinstance(structured, dict):
            return cast(dict[str, Any], structured)
        texts = [
            item.get("text")
            for item in payload.get("content", [])
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        for text in texts:
            if isinstance(text, str):
                try:
                    decoded = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if isinstance(decoded, dict):
                    return cast(dict[str, Any], decoded)
        return {"content": [str(text)[:2_000] for text in texts if text is not None]}


class HTTPJSONConnector:
    """Collect normalized evidence from an explicitly configured HTTPS JSON feed."""

    def __init__(
        self,
        *,
        connector_id: str,
        display_name: str,
        capability: str,
        url: str | None,
        access_credential: str | None,
        handoff_url: str | None = None,
        auth_scheme: str = "Bearer",
        timeout_ms: int = 15_000,
        data_classification: DataClassification = "restricted",
        ca_bundle: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        enabled = bool(url and access_credential)
        if enabled:
            parsed = urlsplit(cast(str, url))
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
            ):
                raise ValueError("HTTP evidence connector URL must be credential-free HTTPS")
            provenance = f"https://{parsed.netloc}{parsed.path}"
        else:
            provenance = f"connector://{connector_id}/unconfigured"
        self.url = url
        self._access_credential = access_credential
        self.auth_scheme = auth_scheme
        self.capability = capability
        self._transport = transport
        self._verify: bool | ssl.SSLContext = True
        if ca_bundle:
            self._verify = ssl.create_default_context(cafile=str(Path(ca_bundle).expanduser()))
        self.descriptor = ConnectorDescriptor(
            id=connector_id,
            display_name=display_name,
            kind="https-json",
            enabled=enabled,
            state="available" if enabled else "unconfigured",
            timeout_ms=timeout_ms,
            data_classification=data_classification,
            capabilities=(capability,),
            provenance=provenance,
            handoff_url=_safe_handoff_url(handoff_url),
        )

    async def collect(
        self,
        capability: str,
        context: dict[str, Any],
    ) -> Sequence[ConnectorEvidence]:
        if capability != self.capability:
            raise ValueError(f"Unsupported {self.descriptor.id} capability: {capability}")
        if (
            not self.descriptor.enabled
            or self.url is None
            or self._access_credential is None
        ):
            raise RuntimeError(f"{self.descriptor.display_name} connector is not configured")
        params = {
            key: value
            for key, value in context.items()
            if key in _ALLOWED_CONTEXT_KEYS and isinstance(value, (str, int, float, bool))
        }
        headers = {"Authorization": f"{self.auth_scheme} {self._access_credential}"}
        async with httpx.AsyncClient(
            headers=headers,
            timeout=self.descriptor.timeout_ms / 1_000,
            verify=self._verify,
            transport=self._transport,
        ) as client:
            response = await client.get(self.url, params=params)
            response.raise_for_status()
            payload = response.json()
        if isinstance(payload, dict):
            raw_items = payload.get("items", payload.get("data", payload.get("results", payload)))
        else:
            raw_items = payload
        if not isinstance(raw_items, list):
            raw_items = [raw_items]
        evidence = []
        for index, raw_item in enumerate(raw_items[:100]):
            item = raw_item if isinstance(raw_item, dict) else {"value": raw_item}
            evidence.append(
                ConnectorEvidence(
                    id=str(item.get("id", item.get("event_id", index))),
                    connector_id=self.descriptor.id,
                    capability=capability,
                    title=str(item.get("title", item.get("name", capability)))[:500],
                    severity=str(item.get("severity", "unknown")),
                    data=cast(dict[str, Any], sanitize_investigation_data(item)),
                    provenance=self.descriptor.provenance,
                )
            )
        return evidence


class ThousandEyesConnector:
    """Normalize ThousandEyes alerts, tests, and path evidence from MCP tools."""

    def __init__(
        self,
        *,
        caller: MCPToolCaller | None,
        enabled: bool,
        timeout_ms: int = 30_000,
        alert_tool: str = "get_alerts",
        test_tool: str = "get_tests",
        path_tool: str = "get_path_evidence",
        handoff_url: str | None = None,
    ) -> None:
        self.caller = caller
        self.alert_tool = alert_tool
        self.test_tool = test_tool
        self.path_tool = path_tool
        self.descriptor = ConnectorDescriptor(
            id="thousandeyes",
            display_name="Cisco ThousandEyes",
            kind="assurance-mcp",
            enabled=enabled,
            state="available" if enabled else "unconfigured",
            timeout_ms=timeout_ms,
            data_classification="restricted",
            capabilities=("alerts", "tests", "path-evidence"),
            provenance="mcp://thousandeyes",
            handoff_url=_safe_handoff_url(handoff_url),
        )

    async def collect(
        self,
        capability: str,
        context: dict[str, Any],
    ) -> Sequence[ConnectorEvidence]:
        if not self.descriptor.enabled or self.caller is None:
            raise RuntimeError("ThousandEyes connector is not configured")
        tool_by_capability = {
            "alerts": self.alert_tool,
            "tests": self.test_tool,
            "path-evidence": self.path_tool,
        }
        try:
            tool = tool_by_capability[capability]
        except KeyError as exc:
            raise ValueError(f"Unsupported ThousandEyes capability: {capability}") from exc
        arguments = {
            key: value
            for key, value in context.items()
            if key in _ALLOWED_CONTEXT_KEYS
        }
        payload = await self.caller.call_tool(tool, arguments)
        raw_items = payload.get(capability.replace("-", "_"), payload.get("data", payload.get("items")))
        if raw_items is None and capability == "alerts":
            raw_items = payload.get("alerts")
        if not isinstance(raw_items, list):
            raw_items = [payload]
        evidence = []
        for index, raw_item in enumerate(raw_items[:100]):
            item = raw_item if isinstance(raw_item, dict) else {"value": raw_item}
            item_id = str(item.get("id", item.get("alertId", item.get("testId", index))))
            title = str(
                item.get(
                    "title",
                    item.get("alertType", item.get("testName", item.get("name", capability))),
                )
            )
            evidence.append(
                ConnectorEvidence(
                    id=item_id,
                    connector_id="thousandeyes",
                    capability=capability,
                    title=title[:500],
                    severity=str(item.get("severity", "unknown")),
                    data=cast(dict[str, Any], sanitize_investigation_data(item)),
                    provenance=f"mcp://thousandeyes/{tool}",
                )
            )
        return evidence


def _timeout_from_environment() -> int:
    try:
        timeout = int(os.getenv("THOUSANDEYES_MCP_TIMEOUT_MS", "30000"))
    except ValueError:
        return 30_000
    return min(max(timeout, 1_000), 120_000)


def _safe_handoff_url(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    return value.split("#", maxsplit=1)[0]


def _http_connector_from_environment(
    *,
    connector_id: str,
    display_name: str,
    capability: str,
    url_name: str,
    credential_env_name: str,
    auth_scheme_name: str,
    handoff_url_name: str,
    data_classification: DataClassification,
) -> HTTPJSONConnector:
    configured_url = os.getenv(url_name) or None
    access_credential = os.getenv(credential_env_name) or None
    auth_scheme = os.getenv(auth_scheme_name, "Bearer")
    ca_bundle = os.getenv("CONNECTOR_CA_BUNDLE") or None
    try:
        return HTTPJSONConnector(
            connector_id=connector_id,
            display_name=display_name,
            capability=capability,
            url=configured_url,
            access_credential=access_credential,
            auth_scheme=auth_scheme,
            handoff_url=os.getenv(handoff_url_name) or None,
            ca_bundle=ca_bundle,
            data_classification=data_classification,
        )
    except (OSError, ValueError):
        return HTTPJSONConnector(
            connector_id=connector_id,
            display_name=display_name,
            capability=capability,
            url=None,
            access_credential=None,
            auth_scheme=auth_scheme,
            handoff_url=None,
            data_classification=data_classification,
        )


def build_default_registry() -> ConnectorRegistry:
    """Build the default registry without initializing unconfigured transports."""
    registry = ConnectorRegistry()
    registry.register(PassiveVManageConnector())
    url = os.getenv("THOUSANDEYES_MCP_URL", "").strip()
    token = os.getenv("THOUSANDEYES_MCP_TOKEN", "").strip()
    timeout_ms = _timeout_from_environment()
    caller: MCPToolCaller | None = None
    enabled = bool(url and token)
    if enabled:
        try:
            caller = StreamableHTTPMCPToolCaller(
                url,
                token,
                timeout_seconds=timeout_ms / 1_000,
                ca_bundle=os.getenv("THOUSANDEYES_CA_BUNDLE") or None,
            )
        except (OSError, ValueError):
            enabled = False
    registry.register(
        ThousandEyesConnector(
            caller=caller,
            enabled=enabled,
            timeout_ms=timeout_ms,
            alert_tool=os.getenv("THOUSANDEYES_ALERT_TOOL", "get_alerts"),
            test_tool=os.getenv("THOUSANDEYES_TEST_TOOL", "get_tests"),
            path_tool=os.getenv("THOUSANDEYES_PATH_TOOL", "get_path_evidence"),
            handoff_url=os.getenv("THOUSANDEYES_HANDOFF_URL") or None,
        )
    )
    registry.register(
        _http_connector_from_environment(
            connector_id="splunk",
            display_name="Splunk",
            capability="events",
            url_name="SPLUNK_EVIDENCE_URL",
            credential_env_name="SPLUNK_EVIDENCE_TOKEN",
            auth_scheme_name="SPLUNK_EVIDENCE_AUTH_SCHEME",
            handoff_url_name="SPLUNK_HANDOFF_URL",
            data_classification="sensitive",
        )
    )
    registry.register(
        _http_connector_from_environment(
            connector_id="appdynamics",
            display_name="Cisco AppDynamics",
            capability="application-evidence",
            url_name="APPDYNAMICS_EVIDENCE_URL",
            credential_env_name="APPDYNAMICS_EVIDENCE_TOKEN",
            auth_scheme_name="APPDYNAMICS_EVIDENCE_AUTH_SCHEME",
            handoff_url_name="APPDYNAMICS_HANDOFF_URL",
            data_classification="restricted",
        )
    )
    return registry
