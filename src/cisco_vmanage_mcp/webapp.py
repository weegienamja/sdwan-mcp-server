"""Local browser workspace for AI-assisted Cisco SD-WAN operations."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol, TypeVar, cast
from urllib.parse import urlsplit

import uvicorn
from pydantic import ValidationError
from pydantic_settings import SettingsError
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import Receive, Scope, Send

from cisco_vmanage_mcp import __version__
from cisco_vmanage_mcp.client import VManageClient
from cisco_vmanage_mcp.services.browser_ai import (
    BrowserExplainer,
    build_browser_explainer,
)
from cisco_vmanage_mcp.services.change_plans import (
    ChangeOperation,
    ChangePlanStore,
    ChangePlanStoreError,
    ExecutionDisabledError,
)
from cisco_vmanage_mcp.services.connected_context import (
    collect_action_inbox,
    collect_topology,
)
from cisco_vmanage_mcp.services.connectors import (
    ConnectorRegistry,
    build_default_registry,
)
from cisco_vmanage_mcp.services.correlation import (
    CorrelationReport,
    correlate_fabric_state,
    diagnose_device,
)
from cisco_vmanage_mcp.services.deployment import (
    DeploymentStateError,
    WebDeploymentSettings,
    bind_state_directory,
    is_loopback_host,
)
from cisco_vmanage_mcp.services.governance import (
    AuthorizationError,
    ConflictError,
    GovernanceActor,
    GovernanceStore,
    GovernanceStoreError,
    HypothesisConfidence,
    RoleAudience,
    build_role_view,
    default_read_only_agents,
)
from cisco_vmanage_mcp.services.identity import IdentityVerifier, OidcIdentityVerifier
from cisco_vmanage_mcp.services.investigations import (
    Investigation,
    InvestigationStore,
    sanitize_investigation_data,
)
from cisco_vmanage_mcp.services.observability import export_snapshot_metrics
from cisco_vmanage_mcp.services.qualification import qualify_environment
from cisco_vmanage_mcp.services.setup import (
    LocalSetupService,
    SetupError,
    SetupService,
    VManageSetupInput,
)
from cisco_vmanage_mcp.services.snapshots import (
    ConfigurationSnapshotClient,
    FabricSnapshot,
    SnapshotStore,
    SnapshotStoreError,
    build_fabric_snapshot,
    build_sla_trends,
    collect_configuration_hashes,
    collect_sla_samples,
    compare_snapshots,
    snapshot_freshness,
)
from cisco_vmanage_mcp.services.web_security import (
    BrowserAuditMiddleware,
    BrowserIdentityMiddleware,
    BrowserResponseSecurityMiddleware,
    request_actor,
)
from cisco_vmanage_mcp.services.workflows import (
    WorkflowDestination,
    WorkflowStore,
    WorkflowStoreError,
)
from cisco_vmanage_mcp.utils.errors import handle_api_error

ASSET_DIR = Path(__file__).with_name("web_assets")
SYSTEM_IP_PATTERN = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
TOOL_COUNT = 21
MAX_MESSAGE_LENGTH = 2_000
MAX_REQUEST_BODY_BYTES = 1_048_576
StoreT = TypeVar("StoreT")
LOCAL_ACTOR = GovernanceActor(
    id="local-owner",
    tenant_id="local",
    display_name="Local owner",
    role="owner",
)


def _create_default_investigation_store(state_dir: Path | None = None) -> InvestigationStore:
    resolved = state_dir or Path(
        os.getenv("VMANAGE_WEB_STATE_DIR", "~/.vmanage-mcp/browser")
    ).expanduser()
    return InvestigationStore(resolved)


def _create_default_snapshot_store(state_dir: Path | None = None) -> SnapshotStore:
    resolved = state_dir or Path(
        os.getenv("VMANAGE_WEB_STATE_DIR", "~/.vmanage-mcp/browser")
    ).expanduser()
    return SnapshotStore(resolved)


def _create_default_workflow_store(state_dir: Path | None = None) -> WorkflowStore:
    resolved = state_dir or Path(
        os.getenv("VMANAGE_WEB_STATE_DIR", "~/.vmanage-mcp/browser")
    ).expanduser()
    return WorkflowStore(resolved)


def _create_default_governance_store(
    bootstrap_actor: GovernanceActor | None = LOCAL_ACTOR,
    state_dir: Path | None = None,
) -> GovernanceStore:
    resolved = state_dir or Path(
        os.getenv("VMANAGE_WEB_STATE_DIR", "~/.vmanage-mcp/browser")
    ).expanduser()
    store = GovernanceStore(resolved, bootstrap_owner=bootstrap_actor)
    if bootstrap_actor is not None:
        for agent in default_read_only_agents(bootstrap_actor.id, bootstrap_actor.tenant_id):
            try:
                store.register_actor(bootstrap_actor.id, agent)
            except ValueError:
                continue
    return store


def _create_default_change_plan_store(state_dir: Path | None = None) -> ChangePlanStore:
    resolved = state_dir or Path(
        os.getenv("VMANAGE_WEB_STATE_DIR", "~/.vmanage-mcp/browser")
    ).expanduser()
    return ChangePlanStore(resolved)


def _browser_error(exc: Exception) -> str:
    """Return an actionable browser error with credential-shaped text redacted."""
    return str(sanitize_investigation_data(handle_api_error(exc)))


def _source_stale_after_seconds() -> int:
    try:
        value = int(os.getenv("VMANAGE_SOURCE_STALE_SECONDS", "300"))
    except ValueError:
        return 300
    return min(max(value, 1), 86_400)


async def _lazy_store(
    request: Request,
    *,
    state_name: str,
    factory: Callable[[], StoreT],
    label: str,
    error_types: tuple[type[Exception], ...],
) -> StoreT | JSONResponse:
    """Return an injected store or initialize its default once under a lock."""
    store = getattr(request.app.state, state_name)
    if store is not None:
        return cast(StoreT, store)
    lock = getattr(request.app.state, f"{state_name}_lock")
    async with lock:
        store = getattr(request.app.state, state_name)
        if store is None:
            try:
                store = await run_in_threadpool(factory)
            except error_types as exc:
                return JSONResponse(
                    {"error": f"{label} storage is unavailable: {type(exc).__name__}"},
                    status_code=500,
                )
            setattr(request.app.state, state_name, store)
    return cast(StoreT, store)


def _manager_url(client: BrowserClient | None) -> str | None:
    """Return a credential-free HTTPS origin for explicit vManage handoff."""
    candidate = getattr(client, "base_url", None)
    if not isinstance(candidate, str):
        return None
    parsed = urlsplit(candidate)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    hostname = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    return f"https://{hostname}{f':{port}' if port is not None else ''}"


class BrowserClient(Protocol):
    """vManage operations required by the browser workspace."""

    async def get(self, endpoint: str, params: dict | None = None) -> dict: ...

    async def close(self) -> None: ...


ClientFactory = Callable[[], BrowserClient]


def _source_evidence(report: CorrelationReport) -> list[dict[str, Any]]:
    return [
        {
            "label": result.source.value,
            "state": "ok" if result.success else "failed",
            "duration_ms": result.duration_ms,
            "detail": result.error,
        }
        for result in report.fabric_report.fetch_results
    ]


def _overview_payload(report: CorrelationReport) -> dict[str, Any]:
    fabric = report.fabric_report
    unreachable = [device for device in fabric.devices if not device.reachable]
    controllers = [device for device in fabric.devices if device.device_type != "vedge"]
    edges = [device for device in fabric.devices if device.device_type == "vedge"]
    return {
        "health": fabric.overall_health.value,
        "partial": fabric.partial,
        "devices": {
            "total": len(fabric.devices),
            "controllers": len(controllers),
            "edges": len(edges),
            "unreachable": len(unreachable),
        },
        "alarms": fabric.alarm_counts,
        "impact": {
            "scope": report.impact.scope if report.impact else "unknown",
            "summary": report.impact.estimated_user_impact if report.impact else "",
            "affected_sites": report.impact.affected_sites if report.impact else [],
        },
        "root_causes": [
            {
                "rank": cause.rank,
                "title": cause.hypothesis,
                "confidence": cause.confidence,
                "evidence": [item for item in cause.supporting_evidence if item],
                "checks": cause.suggested_checks,
            }
            for cause in report.root_causes
        ],
        "sources": _source_evidence(report),
        "updated_at": datetime.fromtimestamp(fabric.timestamp, tz=UTC).isoformat(),
    }


class EvidenceAssistant:
    """Route natural-language operations questions to deterministic evidence."""

    def __init__(self, client: BrowserClient) -> None:
        self.client = client

    async def overview(self) -> dict[str, Any]:
        report = await correlate_fabric_state(cast(VManageClient, self.client))
        return _overview_payload(report)

    async def topology(self) -> dict[str, Any]:
        """Return the normalized current topology with source citations."""
        topology = await collect_topology(self.client)
        return cast(dict[str, Any], topology.model_dump(mode="json"))

    async def actions(self) -> dict[str, Any]:
        """Return grouped current alarms and events in priority order."""
        inbox = await collect_action_inbox(self.client)
        return cast(dict[str, Any], inbox.model_dump(mode="json"))

    async def capture_snapshot(
        self,
        *,
        include_configuration_hashes: bool = False,
    ) -> FabricSnapshot:
        """Capture current normalized evidence as an immutable snapshot."""
        overview, topology = await asyncio.gather(
            self.overview(),
            collect_topology(self.client),
        )
        sla_result, configuration_result = await asyncio.gather(
            collect_sla_samples(self.client, topology),
            collect_configuration_hashes(
                cast(ConfigurationSnapshotClient, self.client),
                topology,
            )
            if include_configuration_hashes
            else asyncio.sleep(0, result=({}, False, ())),
        )
        sla_samples, sla_partial, sla_sources = sla_result
        configuration_hashes, configuration_partial, configuration_sources = (
            configuration_result
        )
        return build_fabric_snapshot(
            overview,
            topology,
            sla_samples=sla_samples,
            sla_partial=sla_partial,
            sla_sources=sla_sources,
            configuration_hashes=configuration_hashes,
            configuration_partial=configuration_partial,
            configuration_sources=configuration_sources,
        )

    async def ask(self, message: str) -> dict[str, Any]:
        lowered = message.lower()
        system_ip = (match.group(0) if (match := SYSTEM_IP_PATTERN.search(message)) else None)

        if system_ip and any(word in lowered for word in ("diagnose", "inspect", "investigate")):
            return await self._diagnose(system_ip)
        if "alarm" in lowered or "alert" in lowered:
            return await self._alarms(critical_only="critical" in lowered)
        if "device" in lowered or "router" in lowered or "unreachable" in lowered:
            return await self._devices(unreachable_only="unreachable" in lowered)
        if any(phrase in lowered for phrase in ("safe to", "pre-change", "make a change", "push")):
            return await self._change_readiness()
        return await self._health()

    async def stream(self, message: str) -> AsyncIterator[dict[str, Any]]:
        """Yield bounded progress metadata followed by a normalized result."""
        yield {"phase": "accepted"}
        yield {"phase": "collecting"}
        yield {"phase": "correlating"}
        try:
            result = await self.ask(message)
        except Exception as exc:
            yield {"phase": "error", "error": _browser_error(exc)}
            return
        yield {"phase": "completed", "result": result}

    async def _health(self) -> dict[str, Any]:
        report = await correlate_fabric_state(cast(VManageClient, self.client))
        overview = _overview_payload(report)
        state = overview["health"]
        affected = overview["devices"]["unreachable"]
        return {
            "kind": "health",
            "status": state,
            "answer": (
                f"Fabric health is {state.upper()}. "
                f"{affected} device{' is' if affected == 1 else 's are'} currently unreachable."
            ),
            "data": overview,
            "evidence": overview["sources"],
            "suggestions": [
                "Show unreachable devices",
                "Show critical alarms",
                "Is it safe to make a change?",
            ],
        }

    async def _devices(self, *, unreachable_only: bool) -> dict[str, Any]:
        payload = await self.client.get("/dataservice/device")
        devices = payload.get("data", [])
        if unreachable_only:
            devices = [
                device
                for device in devices
                if device.get("reachability", "").lower() == "unreachable"
            ]
        normalized = [
            {
                "hostname": device.get("host-name", "N/A"),
                "system_ip": device.get("system-ip", "N/A"),
                "type": device.get("device-type", "N/A"),
                "model": device.get("device-model", "N/A"),
                "site_id": str(device.get("site-id", "N/A")),
                "reachability": device.get("reachability", "unknown"),
                "state": device.get("state", "unknown"),
            }
            for device in devices
        ]
        qualifier = "unreachable " if unreachable_only else ""
        return {
            "kind": "devices",
            "status": "critical" if unreachable_only and normalized else "healthy",
            "answer": f"I found {len(normalized)} {qualifier}device{'s' if len(normalized) != 1 else ''}.",
            "data": {"count": len(normalized), "items": normalized},
            "evidence": [{"label": "GET /dataservice/device", "state": "ok"}],
            "suggestions": ["Assess fabric health", "Show critical alarms"],
        }

    async def _alarms(self, *, critical_only: bool) -> dict[str, Any]:
        payload = await self.client.get("/dataservice/alarms")
        alarms = payload.get("data", [])
        if critical_only:
            alarms = [alarm for alarm in alarms if alarm.get("severity", "").lower() == "critical"]
        normalized = [
            {
                "id": str(
                    alarm.get("uuid")
                    or alarm.get("alarm_uuid")
                    or f"{alarm.get('system-ip', alarm.get('system_ip', 'unknown'))}:{index}"
                ),
                "severity": alarm.get("severity", "Unknown"),
                "type": alarm.get("type", alarm.get("rule_name_display", "Unknown")),
                "hostname": alarm.get("host-name", alarm.get("host_name", "N/A")),
                "system_ip": alarm.get("system-ip", alarm.get("system_ip", "N/A")),
                "message": (alarm.get("message", "") or "")[:240],
            }
            for index, alarm in enumerate(alarms[:50])
        ]
        critical = sum(1 for alarm in normalized if alarm["severity"].lower() == "critical")
        return {
            "kind": "alarms",
            "status": "critical" if critical else "healthy",
            "answer": f"I found {len(normalized)} matching alarms, including {critical} critical.",
            "data": {"count": len(normalized), "items": normalized},
            "evidence": [{"label": "GET /dataservice/alarms", "state": "ok"}],
            "suggestions": ["Assess fabric health", "Show unreachable devices"],
        }

    async def _diagnose(self, system_ip: str) -> dict[str, Any]:
        report, device = await diagnose_device(cast(VManageClient, self.client), system_ip)
        if device is None:
            return {
                "kind": "diagnosis",
                "status": "unknown",
                "answer": f"No device matched system IP {system_ip}.",
                "data": {"device": None},
                "evidence": _source_evidence(report),
                "suggestions": ["Show devices"],
            }
        return {
            "kind": "diagnosis",
            "status": device.overall_health.value,
            "answer": (
                f"{device.hostname} is {device.overall_health.value.upper()} at site "
                f"{device.site_id}. I found {len(device.signals)} grounded health signals."
            ),
            "data": {
                "device": {
                    "hostname": device.hostname,
                    "system_ip": device.system_ip,
                    "site_id": device.site_id,
                    "model": device.device_model,
                    "health": device.overall_health.value,
                    "reachable": device.reachable,
                    "bfd_sessions": device.bfd_sessions,
                    "control_connections": device.control_connections,
                    "signals": [
                        {
                            "level": signal.level.value,
                            "summary": signal.summary,
                            "detail": signal.detail,
                            "source": signal.source.value,
                        }
                        for signal in device.signals
                    ],
                },
                "impact": report.impact.scope if report.impact else "unknown",
                "root_causes": [cause.hypothesis for cause in report.root_causes],
            },
            "evidence": _source_evidence(report),
            "suggestions": ["Assess fabric health", "Is it safe to make a change?"],
        }

    async def _change_readiness(self) -> dict[str, Any]:
        report = await correlate_fabric_state(cast(VManageClient, self.client))
        overview = _overview_payload(report)
        critical_alarms = overview["alarms"].get("Critical", 0)
        if overview["partial"] or overview["health"] in ("critical", "unknown") or critical_alarms:
            status = "no-go"
            answer = "Do not proceed. Critical or incomplete fabric evidence must be resolved first."
        elif overview["health"] == "degraded":
            status = "caution"
            answer = "Proceed only with an approved risk exception; the fabric is degraded."
        else:
            status = "go"
            answer = "Current read-only evidence indicates the fabric is healthy enough to proceed."
        return {
            "kind": "change-readiness",
            "status": status,
            "answer": answer,
            "data": overview,
            "evidence": overview["sources"],
            "suggestions": ["Assess fabric health", "Show critical alarms"],
        }


def create_web_app(
    client_factory: ClientFactory | None = None,
    investigation_store: InvestigationStore | None = None,
    snapshot_store: SnapshotStore | None = None,
    connector_registry: ConnectorRegistry | None = None,
    workflow_store: WorkflowStore | None = None,
    browser_explainer: BrowserExplainer | None = None,
    governance_store: GovernanceStore | None = None,
    change_plan_store: ChangePlanStore | None = None,
    deployment_settings: WebDeploymentSettings | None = None,
    identity_verifier: IdentityVerifier | None = None,
    setup_service: SetupService | None = None,
) -> Starlette:
    """Create the local browser application with injectable dependencies."""
    factory = client_factory or VManageClient
    settings = deployment_settings or WebDeploymentSettings()
    verifier = identity_verifier
    if settings.auth_mode == "oidc" and verifier is None:
        verifier = OidcIdentityVerifier(settings)
    configured_setup_service = setup_service or LocalSetupService()

    @asynccontextmanager
    async def lifespan(app: Starlette):
        client: BrowserClient | None = None
        try:
            client = factory()
            app.state.assistant = EvidenceAssistant(client)
            app.state.startup_error = None
        except Exception as exc:
            app.state.assistant = None
            app.state.startup_error = _browser_error(exc)
        app.state.active_client = client
        app.state.connection_lock = asyncio.Lock()
        app.state.setup_service = configured_setup_service
        app.state.investigation_store = investigation_store
        app.state.investigation_store_lock = asyncio.Lock()
        app.state.snapshot_store = snapshot_store
        app.state.snapshot_store_lock = asyncio.Lock()
        app.state.connector_registry = connector_registry or build_default_registry()
        app.state.workflow_store = workflow_store
        app.state.workflow_store_lock = asyncio.Lock()
        app.state.governance_store = governance_store
        app.state.governance_store_lock = asyncio.Lock()
        app.state.actor_sync_lock = asyncio.Lock()
        app.state.synchronized_actors = {}
        app.state.change_plan_store = change_plan_store
        app.state.change_plan_store_lock = asyncio.Lock()
        app.state.deployment_settings = settings
        app.state.browser_explainer = (
            browser_explainer
            if browser_explainer is not None
            else await run_in_threadpool(build_browser_explainer)
        )
        try:
            yield
        finally:
            active_client = app.state.active_client
            if active_client is not None:
                await active_client.close()
            explainer = app.state.browser_explainer
            close_explainer = getattr(explainer, "close", None)
            if callable(close_explainer):
                await run_in_threadpool(close_explainer)

    async def index(_request: Request) -> Response:
        return FileResponse(ASSET_DIR / "index.html")

    async def live(_request: Request) -> Response:
        return JSONResponse({"status": "live"})

    async def ready(request: Request) -> Response:
        ready_state = request.app.state.assistant is not None
        return JSONResponse(
            {"status": "ready" if ready_state else "not-ready"},
            status_code=200 if ready_state else 503,
        )

    async def meta(request: Request) -> Response:
        assistant = request.app.state.assistant
        explainer = request.app.state.browser_explainer
        actor = request_actor(request)
        identity_mode = "local-owner" if settings.auth_mode == "local" else "oidc"
        return JSONResponse({
            "version": __version__,
            "api_version": "v1",
            "tool_count": TOOL_COUNT,
            "assistant_mode": "evidence",
            "assistant_modes": ["evidence", "generative"] if explainer else ["evidence"],
            "browser_ai_provider": getattr(explainer, "provider_name", None),
            "connected": request.app.state.assistant is not None,
            "error": request.app.state.startup_error,
            "manager_url": _manager_url(assistant.client if assistant is not None else None),
            "identity_mode": identity_mode,
            "actor": actor.model_dump(mode="json"),
            "tenant_mode": "single-instance",
            "change_execution_enabled": False,
            "setup_required": assistant is None,
            "setup_url": "/#setup",
            "source_stale_after_seconds": _source_stale_after_seconds(),
        })

    def setup_access(request: Request) -> JSONResponse | None:
        actor = request_actor(request)
        if settings.auth_mode != "local":
            return JSONResponse(
                {"error": "Enterprise connection settings are deployment-managed"},
                status_code=403,
            )
        if actor.role != "owner":
            return JSONResponse({"error": "Actor is not authorized"}, status_code=403)
        return None

    async def setup_status(request: Request) -> Response:
        connection = configured_setup_service.status().model_dump(mode="json")
        active_assistant = request.app.state.assistant
        if active_assistant is not None:
            active_client = active_assistant.client
            connection.update({
                "credentials_configured": True,
                "host": getattr(active_client, "host", connection["host"]),
                "port": int(getattr(active_client, "port", connection["port"])),
                "verify_ssl": getattr(
                    active_client,
                    "verify_ssl",
                    connection["verify_ssl"],
                ),
            })
        if settings.auth_mode != "local":
            connection["config_file"] = "deployment-managed"
        registry = cast(ConnectorRegistry, request.app.state.connector_registry)
        connector_configuration = {
            "vmanage": (),
            "thousandeyes": ("THOUSANDEYES_MCP_URL", "THOUSANDEYES_MCP_TOKEN"),
            "splunk": ("SPLUNK_EVIDENCE_URL", "SPLUNK_EVIDENCE_TOKEN"),
            "appdynamics": (
                "APPDYNAMICS_EVIDENCE_URL",
                "APPDYNAMICS_EVIDENCE_TOKEN",
            ),
        }
        connectors = [
            {
                "id": descriptor.id,
                "name": descriptor.display_name,
                "state": descriptor.state,
                "enabled": descriptor.enabled,
                "optional": descriptor.id != "vmanage",
                "capabilities": list(descriptor.capabilities),
                "configuration_keys": list(
                    connector_configuration.get(descriptor.id, ())
                ),
            }
            for descriptor in registry.describe()
        ]
        connected = active_assistant is not None
        return JSONResponse({
            "schema_version": 1,
            "mode": settings.auth_mode,
            "editable": settings.auth_mode == "local" and request_actor(request).role == "owner",
            "complete": connected,
            "connection": {
                **connection,
                "connected": connected,
                "error": request.app.state.startup_error,
            },
            "identity": {
                "mode": "local-owner" if settings.auth_mode == "local" else "oidc",
                "role": request_actor(request).role,
                "tenant": request_actor(request).tenant_id,
            },
            "integrations": connectors,
            "browser_ai": {
                "configured": request.app.state.browser_explainer is not None,
                "provider": getattr(request.app.state.browser_explainer, "provider_name", None),
                "configuration_keys": ["BROWSER_AI_PROVIDER", "BROWSER_AI_MODEL"],
            },
        })

    async def test_setup_connection(request: Request) -> Response:
        denied = setup_access(request)
        if denied is not None:
            return denied
        body = await json_body(request)
        if isinstance(body, JSONResponse):
            return body
        try:
            values = VManageSetupInput.model_validate(body)
            report = await configured_setup_service.test(values)
        except ValidationError as exc:
            return JSONResponse({"error": str(exc.errors()[0]["msg"])}, status_code=400)
        except Exception as exc:
            return JSONResponse({"error": _browser_error(exc)}, status_code=502)
        return JSONResponse({
            "ready": report.ready,
            "qualification": report.model_dump(mode="json"),
        })

    async def qualify_setup_connection(request: Request) -> Response:
        assistant = assistant_for(request)
        if isinstance(assistant, JSONResponse):
            return assistant
        try:
            report = await qualify_environment(cast(VManageClient, assistant.client))
        except Exception as exc:
            return JSONResponse({"error": _browser_error(exc)}, status_code=502)
        return JSONResponse({
            "ready": report.ready,
            "qualification": report.model_dump(mode="json"),
        })

    async def apply_setup_connection(request: Request) -> Response:
        denied = setup_access(request)
        if denied is not None:
            return denied
        body = await json_body(request)
        if isinstance(body, JSONResponse):
            return body
        try:
            values = VManageSetupInput.model_validate(body)
        except ValidationError as exc:
            return JSONResponse({"error": str(exc.errors()[0]["msg"])}, status_code=400)

        async with request.app.state.connection_lock:
            try:
                client, report = await configured_setup_service.connect(values)
            except Exception as exc:
                return JSONResponse({"error": _browser_error(exc)}, status_code=502)
            if not report.ready:
                await client.close()
                return JSONResponse(
                    {
                        "error": "Required vManage capabilities are unavailable",
                        "qualification": report.model_dump(mode="json"),
                    },
                    status_code=409,
                )
            try:
                await run_in_threadpool(configured_setup_service.save, values)
            except (OSError, SetupError) as exc:
                await client.close()
                return JSONResponse({"error": _browser_error(exc)}, status_code=500)
            previous_client = request.app.state.active_client
            request.app.state.active_client = client
            request.app.state.assistant = EvidenceAssistant(client)
            request.app.state.startup_error = None
            if previous_client is not None:
                await previous_client.close()
        return JSONResponse({
            "saved": True,
            "connected": True,
            "qualification": report.model_dump(mode="json"),
        })

    def assistant_for(request: Request) -> EvidenceAssistant | JSONResponse:
        assistant = request.app.state.assistant
        if assistant is None:
            return JSONResponse(
                {"error": request.app.state.startup_error or "vManage is unavailable"},
                status_code=503,
            )
        return cast(EvidenceAssistant, assistant)

    async def store_for(request: Request) -> InvestigationStore | JSONResponse:
        return await _lazy_store(
            request,
            state_name="investigation_store",
            factory=lambda: _create_default_investigation_store(
                bind_state_directory(settings)
            ),
            label="Investigation",
            error_types=(OSError, ValueError, DeploymentStateError),
        )

    async def snapshot_store_for(request: Request) -> SnapshotStore | JSONResponse:
        return await _lazy_store(
            request,
            state_name="snapshot_store",
            factory=lambda: _create_default_snapshot_store(bind_state_directory(settings)),
            label="Snapshot",
            error_types=(OSError, ValueError, SnapshotStoreError, DeploymentStateError),
        )

    async def workflow_store_for(request: Request) -> WorkflowStore | JSONResponse:
        return await _lazy_store(
            request,
            state_name="workflow_store",
            factory=lambda: _create_default_workflow_store(bind_state_directory(settings)),
            label="Workflow",
            error_types=(OSError, ValueError, WorkflowStoreError, DeploymentStateError),
        )

    async def governance_store_for(request: Request) -> GovernanceStore | JSONResponse:
        actor = request_actor(request)
        selected = await _lazy_store(
            request,
            state_name="governance_store",
            factory=lambda: _create_default_governance_store(
                LOCAL_ACTOR if settings.auth_mode == "local" else None,
                bind_state_directory(settings),
            ),
            label="Governance",
            error_types=(OSError, ValueError, GovernanceStoreError, DeploymentStateError),
        )
        if isinstance(selected, JSONResponse):
            return selected
        fingerprint = actor.model_dump_json()
        if request.app.state.synchronized_actors.get(actor.id) == fingerprint:
            return selected
        try:
            async with request.app.state.actor_sync_lock:
                if request.app.state.synchronized_actors.get(actor.id) != fingerprint:
                    await run_in_threadpool(selected.synchronize_authenticated_actor, actor)
                    if settings.auth_mode == "oidc" and actor.role == "owner":
                        for agent in default_read_only_agents(actor.id, actor.tenant_id):
                            try:
                                await run_in_threadpool(selected.register_actor, actor.id, agent)
                            except ValueError:
                                continue
                    request.app.state.synchronized_actors[actor.id] = fingerprint
        except (AuthorizationError, GovernanceStoreError):
            return JSONResponse({"error": "Actor is not authorized"}, status_code=403)
        return selected

    async def change_plan_store_for(request: Request) -> ChangePlanStore | JSONResponse:
        return await _lazy_store(
            request,
            state_name="change_plan_store",
            factory=lambda: _create_default_change_plan_store(
                bind_state_directory(settings)
            ),
            label="Change plan",
            error_types=(OSError, ValueError, ChangePlanStoreError, DeploymentStateError),
        )

    async def json_body(request: Request) -> dict[str, Any] | JSONResponse:
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                declared_length = int(content_length)
            except ValueError:
                return JSONResponse({"error": "Content-Length is invalid"}, status_code=400)
            if declared_length < 0:
                return JSONResponse({"error": "Content-Length is invalid"}, status_code=400)
            if declared_length > MAX_REQUEST_BODY_BYTES:
                return JSONResponse({"error": "Request body is too large"}, status_code=413)
        payload = bytearray()
        async for chunk in request.stream():
            payload.extend(chunk)
            if len(payload) > MAX_REQUEST_BODY_BYTES:
                return JSONResponse({"error": "Request body is too large"}, status_code=413)
        try:
            body = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JSONResponse({"error": "Request body must be valid JSON"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "Request body must be a JSON object"}, status_code=400)
        return cast(dict[str, Any], body)

    def assistant_mode_for(
        request: Request,
        body: dict[str, Any],
    ) -> Literal["evidence", "generative"] | JSONResponse:
        mode = body.get("assistant_mode", "evidence")
        if mode not in {"evidence", "generative"}:
            return JSONResponse({"error": "assistant_mode is invalid"}, status_code=400)
        if mode == "generative" and request.app.state.browser_explainer is None:
            return JSONResponse(
                {"error": "Generative browser explanations are not configured"},
                status_code=409,
            )
        return cast(Literal["evidence", "generative"], mode)

    async def apply_browser_explanation(
        request: Request,
        question: str,
        result: dict[str, Any],
        mode: Literal["evidence", "generative"],
    ) -> dict[str, Any]:
        enriched = dict(result)
        enriched["assistant_mode"] = "evidence"
        if mode == "evidence":
            return enriched
        explainer = cast(BrowserExplainer, request.app.state.browser_explainer)
        deterministic_answer = str(result.get("answer", ""))
        safe_result = cast(dict[str, Any], sanitize_investigation_data(result))
        try:
            explanation = await run_in_threadpool(
                explainer.explain,
                question,
                safe_result,
            )
        except Exception:
            enriched["explanation_error"] = "Provider explanation unavailable"
            return enriched
        enriched.update({
            "answer": str(sanitize_investigation_data(explanation))[:10_000],
            "deterministic_answer": deterministic_answer,
            "assistant_mode": "generative",
            "provider": explainer.provider_name,
        })
        return enriched

    def investigation_payload(investigation: Investigation) -> dict[str, Any]:
        return cast(dict[str, Any], investigation.model_dump(mode="json"))

    def investigation_summary(investigation: Investigation) -> dict[str, Any]:
        return {
            "id": investigation.id,
            "title": investigation.title,
            "created_at": investigation.created_at.isoformat(),
            "updated_at": investigation.updated_at.isoformat(),
            "message_count": len(investigation.messages),
            "pin_count": len(investigation.pins),
        }

    def snapshot_summary(snapshot: FabricSnapshot) -> dict[str, Any]:
        freshness = snapshot_freshness(
            snapshot,
            stale_after_seconds=_source_stale_after_seconds(),
        )
        return {
            "id": snapshot.id,
            "captured_at": snapshot.captured_at.isoformat(),
            "health": snapshot.health,
            "partial": snapshot.partial,
            "device_count": len(snapshot.devices),
            "critical_alarms": snapshot.alarm_counts.get("Critical", 0),
            "bfd_down": snapshot.bfd_down,
            "control_down": snapshot.control_down,
            "delayed_source_count": freshness.delayed_count,
        }

    async def overview(request: Request) -> Response:
        assistant = assistant_for(request)
        if isinstance(assistant, JSONResponse):
            return assistant
        try:
            return JSONResponse(await assistant.overview())
        except Exception as exc:
            return JSONResponse({"error": _browser_error(exc)}, status_code=502)

    async def topology(request: Request) -> Response:
        assistant = assistant_for(request)
        if isinstance(assistant, JSONResponse):
            return assistant
        try:
            return JSONResponse(await assistant.topology())
        except Exception as exc:
            return JSONResponse({"error": _browser_error(exc)}, status_code=502)

    async def actions(request: Request) -> Response:
        assistant = assistant_for(request)
        if isinstance(assistant, JSONResponse):
            return assistant
        try:
            return JSONResponse(await assistant.actions())
        except Exception as exc:
            return JSONResponse({"error": _browser_error(exc)}, status_code=502)

    async def chat(request: Request) -> Response:
        assistant = assistant_for(request)
        if isinstance(assistant, JSONResponse):
            return assistant
        body = await json_body(request)
        if isinstance(body, JSONResponse):
            return body
        assistant_mode = assistant_mode_for(request, body)
        if isinstance(assistant_mode, JSONResponse):
            return assistant_mode
        message = body.get("message", "")
        if not isinstance(message, str) or not message.strip():
            return JSONResponse({"error": "message is required"}, status_code=400)
        if len(message) > MAX_MESSAGE_LENGTH:
            return JSONResponse(
                {"error": f"message must be {MAX_MESSAGE_LENGTH} characters or fewer"},
                status_code=400,
            )
        investigation_id: str | None = None
        requested_investigation_id = body.get("investigation_id")
        store: InvestigationStore | None = None
        if requested_investigation_id is not None:
            if not isinstance(requested_investigation_id, str) or not requested_investigation_id:
                return JSONResponse(
                    {"error": "investigation_id must be a non-empty string"},
                    status_code=400,
                )
            investigation_id = requested_investigation_id
            selected_store = await store_for(request)
            if isinstance(selected_store, JSONResponse):
                return selected_store
            store = selected_store
            if await run_in_threadpool(store.get, investigation_id) is None:
                return JSONResponse({"error": "Investigation not found"}, status_code=404)
            await run_in_threadpool(
                store.append_message,
                investigation_id,
                "user",
                message.strip(),
            )
        try:
            result = await assistant.ask(message.strip())
        except Exception as exc:
            error = _browser_error(exc)
            if store is not None and investigation_id is not None:
                await run_in_threadpool(
                    store.append_message,
                    investigation_id,
                    "system",
                    error,
                )
            return JSONResponse({"error": error}, status_code=502)
        result = await apply_browser_explanation(
            request,
            message.strip(),
            result,
            assistant_mode,
        )
        if store is not None and investigation_id is not None:
            await run_in_threadpool(
                store.append_message,
                investigation_id,
                "assistant",
                result["answer"],
                evidence=result.get("evidence", []),
            )
            result["investigation_id"] = investigation_id
        return JSONResponse(result)

    async def stream_chat(request: Request) -> Response:
        assistant = assistant_for(request)
        if isinstance(assistant, JSONResponse):
            return assistant
        body = await json_body(request)
        if isinstance(body, JSONResponse):
            return body
        assistant_mode = assistant_mode_for(request, body)
        if isinstance(assistant_mode, JSONResponse):
            return assistant_mode
        message = body.get("message", "")
        if not isinstance(message, str) or not message.strip():
            return JSONResponse({"error": "message is required"}, status_code=400)
        if len(message) > MAX_MESSAGE_LENGTH:
            return JSONResponse(
                {"error": f"message must be {MAX_MESSAGE_LENGTH} characters or fewer"},
                status_code=400,
            )
        investigation_id: str | None = None
        requested_investigation_id = body.get("investigation_id")
        store: InvestigationStore | None = None
        if requested_investigation_id is not None:
            if not isinstance(requested_investigation_id, str) or not requested_investigation_id:
                return JSONResponse(
                    {"error": "investigation_id must be a non-empty string"},
                    status_code=400,
                )
            investigation_id = requested_investigation_id
            selected_store = await store_for(request)
            if isinstance(selected_store, JSONResponse):
                return selected_store
            store = selected_store
            if await run_in_threadpool(store.get, investigation_id) is None:
                return JSONResponse({"error": "Investigation not found"}, status_code=404)
            await run_in_threadpool(
                store.append_message,
                investigation_id,
                "user",
                message.strip(),
            )

        async def events() -> AsyncIterator[str]:
            async for event in assistant.stream(message.strip()):
                if event["phase"] == "completed":
                    if assistant_mode == "generative":
                        progress = {"phase": "explaining"}
                        yield (
                            "event: progress\n"
                            f"data: {json.dumps(progress, separators=(',', ':'))}\n\n"
                        )
                    event["result"] = await apply_browser_explanation(
                        request,
                        message.strip(),
                        event["result"],
                        assistant_mode,
                    )
                if (
                    store is not None
                    and investigation_id is not None
                    and event["phase"] == "completed"
                ):
                    result = event["result"]
                    await run_in_threadpool(
                        store.append_message,
                        investigation_id,
                        "assistant",
                        result["answer"],
                        evidence=result.get("evidence", []),
                    )
                    result["investigation_id"] = investigation_id
                elif (
                    store is not None
                    and investigation_id is not None
                    and event["phase"] == "error"
                ):
                    await run_in_threadpool(
                        store.append_message,
                        investigation_id,
                        "system",
                        event["error"],
                    )
                yield f"event: progress\ndata: {json.dumps(event, separators=(',', ':'))}\n\n"

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    async def list_investigations(request: Request) -> Response:
        store = await store_for(request)
        if isinstance(store, JSONResponse):
            return store
        investigations = await run_in_threadpool(store.list)
        return JSONResponse({"items": [investigation_summary(item) for item in investigations]})

    async def create_investigation(request: Request) -> Response:
        body = await json_body(request)
        if isinstance(body, JSONResponse):
            return body
        title = body.get("title", "New investigation")
        if not isinstance(title, str) or not title.strip():
            return JSONResponse({"error": "title must be a non-empty string"}, status_code=400)
        store = await store_for(request)
        if isinstance(store, JSONResponse):
            return store
        governance = await governance_store_for(request)
        if isinstance(governance, JSONResponse):
            return governance
        try:
            investigation = await run_in_threadpool(store.create, title.strip())
            await run_in_threadpool(
                governance.create_collaboration,
                investigation.id,
                request_actor(request).id,
            )
        except ValidationError as exc:
            return JSONResponse({"error": str(exc.errors()[0]["msg"])}, status_code=400)
        return JSONResponse(investigation_payload(investigation), status_code=201)

    async def get_investigation(request: Request) -> Response:
        store = await store_for(request)
        if isinstance(store, JSONResponse):
            return store
        investigation = await run_in_threadpool(store.get, request.path_params["investigation_id"])
        if investigation is None:
            return JSONResponse({"error": "Investigation not found"}, status_code=404)
        return JSONResponse(investigation_payload(investigation))

    async def delete_investigation(request: Request) -> Response:
        store = await store_for(request)
        if isinstance(store, JSONResponse):
            return store
        deleted = await run_in_threadpool(store.delete, request.path_params["investigation_id"])
        if not deleted:
            return JSONResponse({"error": "Investigation not found"}, status_code=404)
        return Response(status_code=204)

    async def pin_evidence(request: Request) -> Response:
        store = await store_for(request)
        if isinstance(store, JSONResponse):
            return store
        investigation_id = request.path_params["investigation_id"]
        if await run_in_threadpool(store.get, investigation_id) is None:
            return JSONResponse({"error": "Investigation not found"}, status_code=404)
        body = await json_body(request)
        if isinstance(body, JSONResponse):
            return body
        try:
            investigation = await run_in_threadpool(store.pin, investigation_id, body)
        except ValidationError as exc:
            return JSONResponse({"error": str(exc.errors()[0]["msg"])}, status_code=400)
        return JSONResponse(investigation_payload(investigation))

    async def unpin_evidence(request: Request) -> Response:
        store = await store_for(request)
        if isinstance(store, JSONResponse):
            return store
        investigation_id = request.path_params["investigation_id"]
        try:
            removed = await run_in_threadpool(
                store.unpin,
                investigation_id,
                request.path_params["pin_id"],
            )
        except KeyError:
            return JSONResponse({"error": "Investigation not found"}, status_code=404)
        if not removed:
            return JSONResponse({"error": "Pinned evidence not found"}, status_code=404)
        return Response(status_code=204)

    async def export_investigation(request: Request) -> Response:
        export_format = request.query_params.get("format", "markdown")
        if export_format not in {"json", "markdown"}:
            return JSONResponse(
                {"error": "format must be 'json' or 'markdown'"},
                status_code=400,
            )
        validated_format: Literal["json", "markdown"] = (
            "json" if export_format == "json" else "markdown"
        )
        store = await store_for(request)
        if isinstance(store, JSONResponse):
            return store
        investigation_id = request.path_params["investigation_id"]
        try:
            exported = await run_in_threadpool(store.export, investigation_id, validated_format)
        except KeyError:
            return JSONResponse({"error": "Investigation not found"}, status_code=404)
        suffix = "json" if export_format == "json" else "md"
        media_type = "application/json" if export_format == "json" else "text/markdown"
        return Response(
            exported,
            media_type=media_type,
            headers={
                "Content-Disposition": f'attachment; filename="investigation-{investigation_id}.{suffix}"'
            },
        )

    async def list_snapshots(request: Request) -> Response:
        store = await snapshot_store_for(request)
        if isinstance(store, JSONResponse):
            return store
        snapshots = await run_in_threadpool(store.list)
        return JSONResponse({"items": [snapshot_summary(snapshot) for snapshot in snapshots]})

    async def capture_snapshot(request: Request) -> Response:
        assistant = assistant_for(request)
        if isinstance(assistant, JSONResponse):
            return assistant
        store = await snapshot_store_for(request)
        if isinstance(store, JSONResponse):
            return store
        raw_include_hashes = request.query_params.get("include_configuration_hashes", "false")
        if raw_include_hashes not in {"true", "false"}:
            return JSONResponse(
                {"error": "include_configuration_hashes must be 'true' or 'false'"},
                status_code=400,
            )
        try:
            snapshot = await assistant.capture_snapshot(
                include_configuration_hashes=raw_include_hashes == "true"
            )
            await run_in_threadpool(store.add, snapshot)
            await run_in_threadpool(export_snapshot_metrics, snapshot)
        except Exception as exc:
            return JSONResponse({"error": _browser_error(exc)}, status_code=502)
        return JSONResponse(snapshot.model_dump(mode="json"), status_code=201)

    async def get_snapshot(request: Request) -> Response:
        store = await snapshot_store_for(request)
        if isinstance(store, JSONResponse):
            return store
        snapshot = await run_in_threadpool(store.get, request.path_params["snapshot_id"])
        if snapshot is None:
            return JSONResponse({"error": "Snapshot not found"}, status_code=404)
        return JSONResponse(snapshot.model_dump(mode="json"))

    async def delete_snapshot(request: Request) -> Response:
        store = await snapshot_store_for(request)
        if isinstance(store, JSONResponse):
            return store
        deleted = await run_in_threadpool(store.delete, request.path_params["snapshot_id"])
        if not deleted:
            return JSONResponse({"error": "Snapshot not found"}, status_code=404)
        return Response(status_code=204)

    async def compare_snapshot_ids(request: Request) -> Response:
        before_id = request.query_params.get("before")
        after_id = request.query_params.get("after")
        if not before_id or not after_id:
            return JSONResponse(
                {"error": "before and after snapshot IDs are required"},
                status_code=400,
            )
        store = await snapshot_store_for(request)
        if isinstance(store, JSONResponse):
            return store
        before, after = await asyncio.gather(
            run_in_threadpool(store.get, before_id),
            run_in_threadpool(store.get, after_id),
        )
        if before is None or after is None:
            return JSONResponse({"error": "Snapshot not found"}, status_code=404)
        comparison = compare_snapshots(before, after)
        return JSONResponse(comparison.model_dump(mode="json"))

    async def snapshot_trends(request: Request) -> Response:
        raw_days = request.query_params.get("days", "30")
        try:
            days = int(raw_days)
        except ValueError:
            return JSONResponse({"error": "days must be an integer"}, status_code=400)
        if not 1 <= days <= 365:
            return JSONResponse({"error": "days must be between 1 and 365"}, status_code=400)
        store = await snapshot_store_for(request)
        if isinstance(store, JSONResponse):
            return store
        snapshots = await run_in_threadpool(store.list)
        trends = build_sla_trends(snapshots, days=days)
        return JSONResponse(trends.model_dump(mode="json"))

    async def snapshot_freshness_report(request: Request) -> Response:
        snapshot_id = request.query_params.get("snapshot_id")
        if not snapshot_id:
            return JSONResponse({"error": "snapshot_id is required"}, status_code=400)
        raw_threshold = request.query_params.get(
            "stale_after_seconds",
            str(_source_stale_after_seconds()),
        )
        try:
            threshold = int(raw_threshold)
        except ValueError:
            return JSONResponse(
                {"error": "stale_after_seconds must be an integer"},
                status_code=400,
            )
        if not 1 <= threshold <= 86_400:
            return JSONResponse(
                {"error": "stale_after_seconds must be between 1 and 86400"},
                status_code=400,
            )
        store = await snapshot_store_for(request)
        if isinstance(store, JSONResponse):
            return store
        snapshot = await run_in_threadpool(store.get, snapshot_id)
        if snapshot is None:
            return JSONResponse({"error": "Snapshot not found"}, status_code=404)
        report = snapshot_freshness(
            snapshot,
            stale_after_seconds=threshold,
        )
        return JSONResponse(report.model_dump(mode="json"))

    async def list_connectors(request: Request) -> Response:
        registry = cast(ConnectorRegistry, request.app.state.connector_registry)
        return JSONResponse({
            "items": [descriptor.model_dump(mode="json") for descriptor in registry.describe()]
        })

    async def collect_connector_evidence(request: Request) -> Response:
        body = await json_body(request)
        if isinstance(body, JSONResponse):
            return body
        capability = body.get("capability")
        context = body.get("context", {})
        if not isinstance(capability, str) or not capability.strip() or len(capability) > 100:
            return JSONResponse({"error": "capability is required"}, status_code=400)
        if not isinstance(context, dict):
            return JSONResponse({"error": "context must be a JSON object"}, status_code=400)
        registry = cast(ConnectorRegistry, request.app.state.connector_registry)
        connector_ids: set[str] | None = None
        agent_id = body.get("agent_id")
        if agent_id is not None:
            if not isinstance(agent_id, str) or not agent_id:
                return JSONResponse({"error": "agent_id must be a string"}, status_code=400)
            governance = await governance_store_for(request)
            if isinstance(governance, JSONResponse):
                return governance
            connector_ids = {
                descriptor.id
                for descriptor in registry.describe()
                if await run_in_threadpool(
                    governance.authorize_connector,
                    agent_id,
                    descriptor,
                )
            }
        collection = await registry.collect(
            capability.strip(),
            context,
            connector_ids=connector_ids,
        )
        return JSONResponse(collection.model_dump(mode="json"))

    async def get_collaboration(request: Request) -> Response:
        investigation_id = request.path_params["investigation_id"]
        governance = await governance_store_for(request)
        if isinstance(governance, JSONResponse):
            return governance
        try:
            record = await run_in_threadpool(
                governance.get_collaboration,
                investigation_id,
                request_actor(request).id,
            )
        except KeyError:
            investigations = await store_for(request)
            if isinstance(investigations, JSONResponse):
                return investigations
            if await run_in_threadpool(investigations.get, investigation_id) is None:
                return JSONResponse({"error": "Investigation not found"}, status_code=404)
            record = await run_in_threadpool(
                governance.create_collaboration,
                investigation_id,
                request_actor(request).id,
            )
        except AuthorizationError:
            return JSONResponse({"error": "Actor is not authorized"}, status_code=403)
        return JSONResponse(record.model_dump(mode="json"))

    async def add_collaboration_comment(request: Request) -> Response:
        body = await json_body(request)
        if isinstance(body, JSONResponse):
            return body
        comment = body.get("body")
        expected_revision = body.get("expected_revision")
        if not isinstance(comment, str) or not comment.strip():
            return JSONResponse({"error": "body is required"}, status_code=400)
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
            return JSONResponse({"error": "expected_revision must be an integer"}, status_code=400)
        governance = await governance_store_for(request)
        if isinstance(governance, JSONResponse):
            return governance
        try:
            record = await run_in_threadpool(
                governance.add_comment,
                request.path_params["investigation_id"],
                request_actor(request).id,
                comment,
                expected_revision=expected_revision,
            )
        except KeyError:
            return JSONResponse({"error": "Collaboration not found"}, status_code=404)
        except AuthorizationError:
            return JSONResponse({"error": "Actor is not authorized"}, status_code=403)
        except ConflictError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse(record.model_dump(mode="json"))

    async def role_aware_investigation(request: Request) -> Response:
        audience = request.query_params.get("audience", "noc")
        if audience not in {"executive", "noc", "netops", "secops"}:
            return JSONResponse({"error": "audience is invalid"}, status_code=400)
        investigation_id = request.path_params["investigation_id"]
        investigations = await store_for(request)
        if isinstance(investigations, JSONResponse):
            return investigations
        investigation = await run_in_threadpool(investigations.get, investigation_id)
        if investigation is None:
            return JSONResponse({"error": "Investigation not found"}, status_code=404)
        governance = await governance_store_for(request)
        if isinstance(governance, JSONResponse):
            return governance
        try:
            collaboration = await run_in_threadpool(
                governance.get_collaboration,
                investigation_id,
                request_actor(request).id,
            )
        except KeyError:
            collaboration = await run_in_threadpool(
                governance.create_collaboration,
                investigation_id,
                request_actor(request).id,
            )
        view = build_role_view(
            investigation,
            collaboration,
            cast(RoleAudience, audience),
        )
        return JSONResponse(view.model_dump(mode="json"))

    async def add_collaboration_hypothesis(request: Request) -> Response:
        body = await json_body(request)
        if isinstance(body, JSONResponse):
            return body
        title = body.get("title")
        confidence = body.get("confidence")
        expected_revision = body.get("expected_revision")
        if not isinstance(title, str) or not title.strip():
            return JSONResponse({"error": "title is required"}, status_code=400)
        if confidence not in {"low", "medium", "high"}:
            return JSONResponse({"error": "confidence is invalid"}, status_code=400)
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
            return JSONResponse({"error": "expected_revision must be an integer"}, status_code=400)
        governance = await governance_store_for(request)
        if isinstance(governance, JSONResponse):
            return governance
        try:
            record = await run_in_threadpool(
                governance.add_hypothesis,
                request.path_params["investigation_id"],
                request_actor(request).id,
                title,
                confidence=cast(HypothesisConfidence, confidence),
                expected_revision=expected_revision,
            )
        except KeyError:
            return JSONResponse({"error": "Collaboration not found"}, status_code=404)
        except AuthorizationError:
            return JSONResponse({"error": "Actor is not authorized"}, status_code=403)
        except ConflictError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse(record.model_dump(mode="json"))

    async def update_collaboration_hypothesis(request: Request) -> Response:
        body = await json_body(request)
        if isinstance(body, JSONResponse):
            return body
        confidence = body.get("confidence")
        expected_revision = body.get("expected_revision")
        if confidence not in {"low", "medium", "high"}:
            return JSONResponse({"error": "confidence is invalid"}, status_code=400)
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
            return JSONResponse({"error": "expected_revision must be an integer"}, status_code=400)
        governance = await governance_store_for(request)
        if isinstance(governance, JSONResponse):
            return governance
        try:
            record = await run_in_threadpool(
                governance.update_hypothesis,
                request.path_params["investigation_id"],
                request_actor(request).id,
                request.path_params["hypothesis_id"],
                confidence=cast(HypothesisConfidence, confidence),
                expected_revision=expected_revision,
            )
        except KeyError:
            return JSONResponse({"error": "Collaboration or hypothesis not found"}, status_code=404)
        except AuthorizationError:
            return JSONResponse({"error": "Actor is not authorized"}, status_code=403)
        except ConflictError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        return JSONResponse(record.model_dump(mode="json"))

    async def list_agents(request: Request) -> Response:
        governance = await governance_store_for(request)
        if isinstance(governance, JSONResponse):
            return governance
        actors = await run_in_threadpool(governance.list_actors, request_actor(request).id)
        return JSONResponse({
            "items": [actor.model_dump(mode="json") for actor in actors if actor.role == "agent"]
        })

    async def list_actors(request: Request) -> Response:
        governance = await governance_store_for(request)
        if isinstance(governance, JSONResponse):
            return governance
        actors = await run_in_threadpool(governance.list_actors, request_actor(request).id)
        return JSONResponse({
            "items": [actor.model_dump(mode="json") for actor in actors]
        })

    async def assign_collaboration_owner(request: Request) -> Response:
        body = await json_body(request)
        if isinstance(body, JSONResponse):
            return body
        owner_id = body.get("owner_id")
        expected_revision = body.get("expected_revision")
        if not isinstance(owner_id, str) or not owner_id:
            return JSONResponse({"error": "owner_id is required"}, status_code=400)
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
            return JSONResponse({"error": "expected_revision must be an integer"}, status_code=400)
        governance = await governance_store_for(request)
        if isinstance(governance, JSONResponse):
            return governance
        try:
            record = await run_in_threadpool(
                governance.assign_owner,
                request.path_params["investigation_id"],
                request_actor(request).id,
                owner_id,
                expected_revision=expected_revision,
            )
        except KeyError:
            return JSONResponse({"error": "Actor or collaboration not found"}, status_code=404)
        except AuthorizationError:
            return JSONResponse({"error": "Actor is not authorized"}, status_code=403)
        except ConflictError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        return JSONResponse(record.model_dump(mode="json"))

    async def register_agent(request: Request) -> Response:
        body = await json_body(request)
        if isinstance(body, JSONResponse):
            return body
        try:
            actor = GovernanceActor.model_validate({
                "id": body.get("id"),
                "tenant_id": request_actor(request).tenant_id,
                "display_name": body.get("display_name"),
                "role": "agent",
                "human_owner_id": body.get("human_owner_id"),
                "allowed_connectors": body.get("allowed_connectors", []),
                "maximum_data_classification": body.get(
                    "maximum_data_classification",
                    "operational",
                ),
            })
        except ValidationError as exc:
            return JSONResponse({"error": str(exc.errors()[0]["msg"])}, status_code=400)
        governance = await governance_store_for(request)
        if isinstance(governance, JSONResponse):
            return governance
        try:
            registered = await run_in_threadpool(
                governance.register_actor,
                request_actor(request).id,
                actor,
            )
        except AuthorizationError:
            return JSONResponse({"error": "Actor is not authorized"}, status_code=403)
        except (KeyError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        return JSONResponse(registered.model_dump(mode="json"), status_code=201)

    async def list_workflows(request: Request) -> Response:
        store = await workflow_store_for(request)
        if isinstance(store, JSONResponse):
            return store
        drafts = await run_in_threadpool(store.list)
        return JSONResponse({
            "items": [draft.model_dump(mode="json") for draft in drafts]
        })

    async def create_workflow_draft(request: Request) -> Response:
        body = await json_body(request)
        if isinstance(body, JSONResponse):
            return body
        investigation_id = body.get("investigation_id")
        destination = body.get("destination")
        if not isinstance(investigation_id, str) or not investigation_id:
            return JSONResponse({"error": "investigation_id is required"}, status_code=400)
        if destination not in {"servicenow", "webex", "slack"}:
            return JSONResponse({"error": "destination is invalid"}, status_code=400)
        target = body.get("target")
        actor_id = (
            request_actor(request).id
            if settings.auth_mode == "oidc"
            else body.get("actor_id", "local-operator")
        )
        ttl_minutes = body.get("ttl_minutes", 60)
        if target is not None and not isinstance(target, str):
            return JSONResponse({"error": "target must be a string"}, status_code=400)
        if not isinstance(actor_id, str) or not actor_id:
            return JSONResponse({"error": "actor_id is required"}, status_code=400)
        if isinstance(ttl_minutes, bool) or not isinstance(ttl_minutes, int):
            return JSONResponse({"error": "ttl_minutes must be an integer"}, status_code=400)
        investigations = await store_for(request)
        if isinstance(investigations, JSONResponse):
            return investigations
        investigation = await run_in_threadpool(investigations.get, investigation_id)
        if investigation is None:
            return JSONResponse({"error": "Investigation not found"}, status_code=404)
        workflows = await workflow_store_for(request)
        if isinstance(workflows, JSONResponse):
            return workflows
        try:
            draft = await run_in_threadpool(
                workflows.create_draft,
                investigation,
                cast(WorkflowDestination, destination),
                target=target,
                actor_id=actor_id,
                ttl_minutes=ttl_minutes,
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse(draft.model_dump(mode="json"), status_code=201)

    async def get_workflow(request: Request) -> Response:
        store = await workflow_store_for(request)
        if isinstance(store, JSONResponse):
            return store
        draft = await run_in_threadpool(store.get, request.path_params["workflow_id"])
        if draft is None:
            return JSONResponse({"error": "Workflow draft not found"}, status_code=404)
        return JSONResponse(draft.model_dump(mode="json"))

    async def approve_workflow(request: Request) -> Response:
        body = await json_body(request)
        if isinstance(body, JSONResponse):
            return body
        actor_id = (
            request_actor(request).id
            if settings.auth_mode == "oidc"
            else body.get("actor_id")
        )
        expected_hash = body.get("expected_hash")
        if not isinstance(actor_id, str) or not actor_id:
            return JSONResponse({"error": "actor_id is required"}, status_code=400)
        if not isinstance(expected_hash, str) or not expected_hash:
            return JSONResponse({"error": "expected_hash is required"}, status_code=400)
        store = await workflow_store_for(request)
        if isinstance(store, JSONResponse):
            return store
        try:
            draft = await run_in_threadpool(
                store.approve,
                request.path_params["workflow_id"],
                actor_id=actor_id,
                expected_hash=expected_hash,
            )
        except KeyError:
            return JSONResponse({"error": "Workflow draft not found"}, status_code=404)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        return JSONResponse(draft.model_dump(mode="json"))

    async def cancel_workflow(request: Request) -> Response:
        body = await json_body(request)
        if isinstance(body, JSONResponse):
            return body
        actor_id = (
            request_actor(request).id
            if settings.auth_mode == "oidc"
            else body.get("actor_id", "local-operator")
        )
        if not isinstance(actor_id, str) or not actor_id:
            return JSONResponse({"error": "actor_id is required"}, status_code=400)
        store = await workflow_store_for(request)
        if isinstance(store, JSONResponse):
            return store
        try:
            draft = await run_in_threadpool(
                store.cancel,
                request.path_params["workflow_id"],
                actor_id=actor_id,
            )
        except KeyError:
            return JSONResponse({"error": "Workflow draft not found"}, status_code=404)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        return JSONResponse(draft.model_dump(mode="json"))

    async def list_change_plans(request: Request) -> Response:
        store = await change_plan_store_for(request)
        if isinstance(store, JSONResponse):
            return store
        plans = await run_in_threadpool(store.list, actor=request_actor(request))
        return JSONResponse({
            "items": [plan.model_dump(mode="json") for plan in plans]
        })

    async def create_change_plan(request: Request) -> Response:
        body = await json_body(request)
        if isinstance(body, JSONResponse):
            return body
        operation = body.get("operation")
        if operation not in {"attach-device-template", "activate-central-policy"}:
            return JSONResponse({"error": "operation is invalid"}, status_code=400)
        intent = body.get("intent")
        target_id = body.get("target_id")
        targets = body.get("targets")
        canary_targets = body.get("canary_targets", [])
        pre_snapshot_id = body.get("pre_snapshot_id")
        parameters = body.get("parameters", {})
        expected_outcomes = body.get("expected_outcomes", [])
        rollback_strategy = body.get(
            "rollback_strategy",
            "Restore the previous approved state.",
        )
        ttl_minutes = body.get("ttl_minutes", 60)
        if not isinstance(intent, str) or not isinstance(target_id, str):
            return JSONResponse({"error": "intent and target_id are required"}, status_code=400)
        if not isinstance(targets, list) or not all(isinstance(item, str) for item in targets):
            return JSONResponse({"error": "targets must be a string array"}, status_code=400)
        if not isinstance(canary_targets, list) or not all(
            isinstance(item, str) for item in canary_targets
        ):
            return JSONResponse(
                {"error": "canary_targets must be a string array"},
                status_code=400,
            )
        if not isinstance(pre_snapshot_id, str) or not pre_snapshot_id:
            return JSONResponse({"error": "pre_snapshot_id is required"}, status_code=400)
        if not isinstance(parameters, dict) or not isinstance(expected_outcomes, list):
            return JSONResponse(
                {"error": "parameters and expected_outcomes are invalid"},
                status_code=400,
            )
        if not all(isinstance(item, str) for item in expected_outcomes):
            return JSONResponse({"error": "expected_outcomes must be a string array"}, status_code=400)
        if not isinstance(rollback_strategy, str):
            return JSONResponse({"error": "rollback_strategy must be a string"}, status_code=400)
        if isinstance(ttl_minutes, bool) or not isinstance(ttl_minutes, int):
            return JSONResponse({"error": "ttl_minutes must be an integer"}, status_code=400)
        snapshots = await snapshot_store_for(request)
        if isinstance(snapshots, JSONResponse):
            return snapshots
        if await run_in_threadpool(snapshots.get, pre_snapshot_id) is None:
            return JSONResponse({"error": "Pre-change snapshot not found"}, status_code=404)
        plans = await change_plan_store_for(request)
        if isinstance(plans, JSONResponse):
            return plans
        try:
            plan = await run_in_threadpool(
                plans.create,
                actor=request_actor(request),
                operation=cast(ChangeOperation, operation),
                intent=intent,
                target_id=target_id,
                targets=tuple(targets),
                canary_targets=tuple(canary_targets),
                pre_snapshot_id=pre_snapshot_id,
                parameters=parameters,
                expected_outcomes=tuple(expected_outcomes),
                rollback_strategy=rollback_strategy,
                ttl_minutes=ttl_minutes,
            )
        except AuthorizationError:
            return JSONResponse({"error": "Actor is not authorized"}, status_code=403)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse(plan.model_dump(mode="json"), status_code=201)

    async def get_change_plan(request: Request) -> Response:
        store = await change_plan_store_for(request)
        if isinstance(store, JSONResponse):
            return store
        try:
            plan = await run_in_threadpool(
                store.get,
                request.path_params["plan_id"],
                actor=request_actor(request),
            )
        except AuthorizationError:
            return JSONResponse({"error": "Actor is not authorized"}, status_code=403)
        if plan is None:
            return JSONResponse({"error": "Change plan not found"}, status_code=404)
        return JSONResponse(plan.model_dump(mode="json"))

    async def approve_change_plan(request: Request) -> Response:
        body = await json_body(request)
        if isinstance(body, JSONResponse):
            return body
        expected_hash = body.get("expected_hash")
        approval_minutes = body.get("approval_minutes", 15)
        if not isinstance(expected_hash, str) or not expected_hash:
            return JSONResponse({"error": "expected_hash is required"}, status_code=400)
        if isinstance(approval_minutes, bool) or not isinstance(approval_minutes, int):
            return JSONResponse({"error": "approval_minutes must be an integer"}, status_code=400)
        store = await change_plan_store_for(request)
        if isinstance(store, JSONResponse):
            return store
        try:
            plan = await run_in_threadpool(
                store.approve,
                request.path_params["plan_id"],
                actor=request_actor(request),
                expected_hash=expected_hash,
                approval_minutes=approval_minutes,
            )
        except KeyError:
            return JSONResponse({"error": "Change plan not found"}, status_code=404)
        except AuthorizationError:
            return JSONResponse({"error": "Actor is not authorized"}, status_code=403)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        return JSONResponse(plan.model_dump(mode="json"))

    async def verify_change_plan(request: Request) -> Response:
        body = await json_body(request)
        if isinstance(body, JSONResponse):
            return body
        post_snapshot_id = body.get("post_snapshot_id")
        if not isinstance(post_snapshot_id, str) or not post_snapshot_id:
            return JSONResponse({"error": "post_snapshot_id is required"}, status_code=400)
        plans = await change_plan_store_for(request)
        if isinstance(plans, JSONResponse):
            return plans
        try:
            plan = await run_in_threadpool(
                plans.get,
                request.path_params["plan_id"],
                actor=request_actor(request),
            )
        except AuthorizationError:
            return JSONResponse({"error": "Actor is not authorized"}, status_code=403)
        if plan is None:
            return JSONResponse({"error": "Change plan not found"}, status_code=404)
        snapshots = await snapshot_store_for(request)
        if isinstance(snapshots, JSONResponse):
            return snapshots
        before, after = await asyncio.gather(
            run_in_threadpool(snapshots.get, plan.pre_snapshot_id),
            run_in_threadpool(snapshots.get, post_snapshot_id),
        )
        if before is None or after is None:
            return JSONResponse({"error": "Verification snapshot not found"}, status_code=404)
        try:
            verified = await run_in_threadpool(
                plans.verify,
                plan.id,
                actor=request_actor(request),
                before=before,
                after=after,
            )
        except AuthorizationError:
            return JSONResponse({"error": "Actor is not authorized"}, status_code=403)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        return JSONResponse(verified.model_dump(mode="json"))

    async def cancel_change_plan(request: Request) -> Response:
        store = await change_plan_store_for(request)
        if isinstance(store, JSONResponse):
            return store
        try:
            plan = await run_in_threadpool(
                store.cancel,
                request.path_params["plan_id"],
                actor=request_actor(request),
            )
        except KeyError:
            return JSONResponse({"error": "Change plan not found"}, status_code=404)
        except AuthorizationError:
            return JSONResponse({"error": "Actor is not authorized"}, status_code=403)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        return JSONResponse(plan.model_dump(mode="json"))

    async def apply_change_plan(request: Request) -> Response:
        store = await change_plan_store_for(request)
        if isinstance(store, JSONResponse):
            return store
        try:
            await run_in_threadpool(
                store.assert_execution_enabled,
                request.path_params["plan_id"],
            )
        except ExecutionDisabledError as exc:
            return JSONResponse({"error": str(exc)}, status_code=403)
        return JSONResponse({"error": "Execution is unavailable"}, status_code=403)

    middleware = [
        Middleware(
            BrowserResponseSecurityMiddleware,
            enterprise_mode=settings.auth_mode == "oidc",
        ),
        Middleware(BrowserAuditMiddleware),
    ]
    middleware.append(
        Middleware(
            BrowserIdentityMiddleware,
            settings=settings,
            local_actor=LOCAL_ACTOR,
            verifier=verifier,
        )
    )
    return Starlette(
        routes=[
            Route("/health/live", live),
            Route("/health/ready", ready),
            Route("/", index),
            Route("/api/v1/setup", setup_status),
            Route("/api/v1/setup/qualify", qualify_setup_connection, methods=["POST"]),
            Route("/api/v1/setup/test", test_setup_connection, methods=["POST"]),
            Route("/api/v1/setup/apply", apply_setup_connection, methods=["POST"]),
            Route("/api/meta", meta),
            Route("/api/v1/meta", meta),
            Route("/api/overview", overview),
            Route("/api/v1/overview", overview),
            Route("/api/v1/topology", topology),
            Route("/api/v1/actions", actions),
            Route("/api/chat", chat, methods=["POST"]),
            Route("/api/v1/chat", chat, methods=["POST"]),
            Route("/api/v1/chat/stream", stream_chat, methods=["POST"]),
            Route(
                "/api/v1/investigations",
                list_investigations,
                methods=["GET"],
            ),
            Route(
                "/api/v1/investigations",
                create_investigation,
                methods=["POST"],
            ),
            Route(
                "/api/v1/investigations/{investigation_id}",
                get_investigation,
                methods=["GET"],
            ),
            Route(
                "/api/v1/investigations/{investigation_id}",
                delete_investigation,
                methods=["DELETE"],
            ),
            Route(
                "/api/v1/investigations/{investigation_id}/pins",
                pin_evidence,
                methods=["POST"],
            ),
            Route(
                "/api/v1/investigations/{investigation_id}/pins/{pin_id}",
                unpin_evidence,
                methods=["DELETE"],
            ),
            Route(
                "/api/v1/investigations/{investigation_id}/export",
                export_investigation,
                methods=["GET"],
            ),
            Route("/api/v1/snapshots", list_snapshots, methods=["GET"]),
            Route("/api/v1/snapshots", capture_snapshot, methods=["POST"]),
            Route(
                "/api/v1/snapshots/{snapshot_id}",
                get_snapshot,
                methods=["GET"],
            ),
            Route(
                "/api/v1/snapshots/{snapshot_id}",
                delete_snapshot,
                methods=["DELETE"],
            ),
            Route("/api/v1/comparisons", compare_snapshot_ids),
            Route("/api/v1/trends", snapshot_trends),
            Route("/api/v1/freshness", snapshot_freshness_report),
            Route("/api/v1/connectors", list_connectors),
            Route(
                "/api/v1/connectors/collect",
                collect_connector_evidence,
                methods=["POST"],
            ),
            Route("/api/v1/agents", list_agents),
            Route("/api/v1/agents", register_agent, methods=["POST"]),
            Route("/api/v1/actors", list_actors),
            Route(
                "/api/v1/collaborations/{investigation_id}",
                get_collaboration,
            ),
            Route(
                "/api/v1/collaborations/{investigation_id}/comments",
                add_collaboration_comment,
                methods=["POST"],
            ),
            Route(
                "/api/v1/collaborations/{investigation_id}/owner",
                assign_collaboration_owner,
                methods=["POST"],
            ),
            Route(
                "/api/v1/investigations/{investigation_id}/view",
                role_aware_investigation,
            ),
            Route(
                "/api/v1/collaborations/{investigation_id}/hypotheses",
                add_collaboration_hypothesis,
                methods=["POST"],
            ),
            Route(
                "/api/v1/collaborations/{investigation_id}/hypotheses/{hypothesis_id}",
                update_collaboration_hypothesis,
                methods=["PATCH"],
            ),
            Route("/api/v1/workflows", list_workflows),
            Route(
                "/api/v1/workflows/drafts",
                create_workflow_draft,
                methods=["POST"],
            ),
            Route(
                "/api/v1/workflows/{workflow_id}/approve",
                approve_workflow,
                methods=["POST"],
            ),
            Route(
                "/api/v1/workflows/{workflow_id}/cancel",
                cancel_workflow,
                methods=["POST"],
            ),
            Route("/api/v1/workflows/{workflow_id}", get_workflow),
            Route("/api/v1/change-plans", list_change_plans, methods=["GET"]),
            Route("/api/v1/change-plans", create_change_plan, methods=["POST"]),
            Route(
                "/api/v1/change-plans/{plan_id}/approve",
                approve_change_plan,
                methods=["POST"],
            ),
            Route(
                "/api/v1/change-plans/{plan_id}/verify",
                verify_change_plan,
                methods=["POST"],
            ),
            Route(
                "/api/v1/change-plans/{plan_id}/cancel",
                cancel_change_plan,
                methods=["POST"],
            ),
            Route(
                "/api/v1/change-plans/{plan_id}/apply",
                apply_change_plan,
                methods=["POST"],
            ),
            Route("/api/v1/change-plans/{plan_id}", get_change_plan),
            Mount("/assets", StaticFiles(directory=ASSET_DIR), name="assets"),
        ],
        middleware=middleware,
        lifespan=lifespan,
    )


def _load_default_web_app() -> Starlette:
    try:
        settings = WebDeploymentSettings()
    except (SettingsError, ValidationError) as exc:
        raise RuntimeError(f"Invalid browser deployment configuration: {exc}") from exc
    return create_web_app(deployment_settings=settings)


class _LazyWebApplication:
    """Construct the environment-configured ASGI application on first use."""

    def __init__(self) -> None:
        self._application: Starlette | None = None
        self._lock = asyncio.Lock()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self._application is None:
            async with self._lock:
                if self._application is None:
                    self._application = _load_default_web_app()
        await self._application(scope, receive, send)


app = _LazyWebApplication()


def main(args: list[str] | None = None) -> None:
    """Run the browser workspace in validated local or enterprise mode."""
    try:
        settings = WebDeploymentSettings()
    except (SettingsError, ValidationError) as exc:
        raise SystemExit(f"Invalid browser deployment configuration: {exc}") from exc
    parser = argparse.ArgumentParser(description="Cisco SD-WAN browser operations canvas")
    parser.add_argument("--host", default=settings.host)
    parser.add_argument("--port", type=int, default=settings.port)
    parsed = parser.parse_args(args)
    if settings.auth_mode == "local" and not is_loopback_host(parsed.host):
        raise SystemExit("The browser workspace is local-only; bind to a loopback host")
    try:
        settings = WebDeploymentSettings.model_validate({
            **settings.model_dump(),
            "host": parsed.host,
            "port": parsed.port,
        })
    except ValidationError as exc:
        raise SystemExit(f"Invalid browser deployment configuration: {exc}") from exc
    uvicorn.run(
        create_web_app(deployment_settings=settings),
        host=parsed.host,
        port=parsed.port,
        reload=False,
        access_log=False,
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
