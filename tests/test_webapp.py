"""Browser application contracts using an offline vManage client."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime
from unittest.mock import Mock

import pytest
from starlette.testclient import TestClient

from cisco_vmanage_mcp import webapp
from cisco_vmanage_mcp.services.change_plans import ChangePlanStore
from cisco_vmanage_mcp.services.connectors import (
    ConnectorDescriptor,
    ConnectorEvidence,
    ConnectorRegistry,
)
from cisco_vmanage_mcp.services.deployment import WebDeploymentSettings
from cisco_vmanage_mcp.services.governance import GovernanceActor, GovernanceStore
from cisco_vmanage_mcp.services.investigations import InvestigationStore
from cisco_vmanage_mcp.services.qualification import (
    CapabilityCheck,
    EnvironmentQualification,
)
from cisco_vmanage_mcp.services.setup import (
    SetupConnectionStatus,
    SetupError,
    VManageSetupInput,
)
from cisco_vmanage_mcp.services.snapshots import SnapshotStore
from cisco_vmanage_mcp.services.workflows import WorkflowStore
from cisco_vmanage_mcp.webapp import EvidenceAssistant, create_web_app
from tests.test_snapshots import _snapshot


class WebClient:
    host = "vmanage.example.test"
    port = "443"
    base_url = "https://vmanage.example.test:443"

    def __init__(self) -> None:
        self.closed = False

    async def get(self, endpoint: str, params=None) -> dict:
        now = int(time.time() * 1000)
        responses = {
            "/dataservice/device": [
                {
                    "host-name": "vmanage-1",
                    "system-ip": "10.0.0.10",
                    "deviceId": "10.0.0.10",
                    "device-type": "vmanage",
                    "device-model": "vmanage",
                    "reachability": "reachable",
                    "site-id": "1",
                    "state": "green",
                    "bfdSessions": "--",
                    "controlConnections": 2,
                },
                {
                    "host-name": "edge-1",
                    "system-ip": "10.0.0.1",
                    "deviceId": "10.0.0.1",
                    "uuid": "edge-uuid-1",
                    "device-type": "vedge",
                    "device-model": "vedge-C8000V",
                    "reachability": "unreachable",
                    "site-id": "100",
                    "state": "red",
                    "bfdSessions": 0,
                    "controlConnections": 0,
                },
            ],
            "/dataservice/alarms": [
                {
                    "severity": "Critical",
                    "type": "Control",
                    "host-name": "edge-1",
                    "system-ip": "10.0.0.1",
                    "message": "Control connection changed",
                    "entry_time": now,
                }
            ],
            "/dataservice/alarms/count": [
                {"severity": "Critical", "count": 1},
                {"severity": "Major", "count": 0},
            ],
            "/dataservice/event": [
                {
                    "severity_level": "Major",
                    "eventname": "control-connection-down",
                    "host_name": "edge-1",
                    "system_ip": "10.0.0.1",
                    "entry_time": now,
                }
            ],
            "/dataservice/device/bfd/sessions": [],
            "/dataservice/device/control/connections": [],
            "/dataservice/device/app-route/statistics": [
                {
                    "remote-system-ip": "10.0.0.2",
                    "local-color": "biz-internet",
                    "remote-color": "biz-internet",
                    "average-latency": 20,
                    "average-jitter": 2,
                    "loss": 0,
                }
            ],
            "/dataservice/device/system/status": [
                {"min5_avg": 10, "mem_used": 10, "mem_free": 90}
            ],
        }
        return {"data": responses[endpoint]}

    async def close(self) -> None:
        self.closed = True

    async def get_raw(self, endpoint: str, params=None) -> str:
        assert endpoint == "/dataservice/template/config/running/edge-uuid-1"
        return "hostname edge-1\nusername admin password never-return-this"


class StaticIdentityVerifier:
    def __init__(self, actor: GovernanceActor) -> None:
        self.actor = actor

    async def verify(self, token: str) -> GovernanceActor:
        del token
        return self.actor


class BrowserSetupService:
    def __init__(self) -> None:
        self.saved = False
        self.tested = False
        self.connected_client: WebClient | None = None

    def status(self) -> SetupConnectionStatus:
        return SetupConnectionStatus(
            credentials_configured=False,
            config_file_exists=False,
            config_file="/private/setup/vmanage.env",
            host="vmanage.example.test",
            port=443,
            verify_ssl=True,
            ca_bundle_configured=False,
        )

    @staticmethod
    def _report() -> EnvironmentQualification:
        return EnvironmentQualification(
            generated_at=datetime.now().astimezone(),
            manager="vmanage.example.test:443",
            ready=True,
            partial=False,
            device_count=2,
            device_types={"vedge": 1, "vmanage": 1},
            device_models={"C8500-12X": 1, "vManage": 1},
            software_versions={"20.18.2.1": 1, "17.18.01a": 1},
            capabilities=(
                CapabilityCheck(
                    id="inventory",
                    endpoint="/dataservice/device",
                    required=True,
                    status="available",
                    duration_ms=12.3,
                    row_count=2,
                ),
                CapabilityCheck(
                    id="alarms",
                    endpoint="/dataservice/alarms/count",
                    required=True,
                    status="available",
                    duration_ms=8.2,
                    row_count=1,
                ),
            ),
            warnings=(),
        )

    async def test(self, values: VManageSetupInput) -> EnvironmentQualification:
        del values
        self.tested = True
        return self._report()

    async def connect(
        self,
        values: VManageSetupInput,
    ) -> tuple[WebClient, EnvironmentQualification]:
        self.connected_client = WebClient()
        self.connected_client.host = values.host
        self.connected_client.port = str(values.port)
        return self.connected_client, self._report()

    def save(self, values: VManageSetupInput) -> None:
        del values
        self.saved = True


def _oidc_settings() -> WebDeploymentSettings:
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


def _sse_events(response) -> list[dict]:
    return [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]


def test_browser_workspace_and_metadata() -> None:
    client = WebClient()
    with TestClient(create_web_app(client_factory=lambda: client)) as browser:
        page = browser.get("/")
        styles = browser.get("/assets/styles.css")
        script = browser.get("/assets/app.js")
        logo = browser.get("/assets/cisco-logo.svg")
        regular_font = browser.get("/assets/fonts/CiscoSansTTRegular.woff")
        bold_font = browser.get("/assets/fonts/CiscoSansTTBold.ttf")
        metadata = browser.get("/api/meta")

        assert page.status_code == 200
        assert "SD-WAN Operations Canvas" in page.text
        assert 'src="/assets/cisco-logo.svg"' in page.text
        assert "fonts.googleapis.com" not in page.text
        assert 'id="assistant-form"' in page.text
        assert styles.status_code == 200
        assert "--cisco-blue" in styles.text
        assert '@font-face' in styles.text
        assert 'font-family: "CiscoSans"' in styles.text
        assert logo.status_code == 200
        assert 'viewBox="0 0 500 264"' in logo.text
        assert regular_font.status_code == 200
        assert bold_font.status_code == 200
        assert script.status_code == 200
        assert "function renderOverview" in script.text
        assert page.headers["x-frame-options"] == "DENY"
        assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
        assert metadata.status_code == 200
        assert metadata.headers["cache-control"] == "no-store"
        assert metadata.json()["tool_count"] == 21
        assert metadata.json()["assistant_mode"] == "evidence"
        assert metadata.json()["api_version"] == "v1"
        assert metadata.json()["manager_url"] == "https://vmanage.example.test:443"

    assert client.closed is True


def test_setup_api_is_private_detailed_and_reconnects_without_restart() -> None:
    setup = BrowserSetupService()
    initial_client = WebClient()
    values = {
        "host": "new-vmanage.example.test",
        "port": 8443,
        "username": "operator",
        "password": "never-return-this-password",
        "verify_ssl": True,
        "ca_bundle": None,
    }
    application = create_web_app(
        client_factory=lambda: initial_client,
        setup_service=setup,
    )

    with TestClient(application) as browser:
        status = browser.get("/api/v1/setup")
        tested = browser.post("/api/v1/setup/test", json=values)
        applied = browser.post("/api/v1/setup/apply", json=values)
        refreshed_status = browser.get("/api/v1/setup")
        metadata = browser.get("/api/v1/meta")

        assert setup.connected_client is not None
        assert setup.connected_client.closed is False

    assert status.status_code == 200
    assert status.json()["editable"] is True
    assert status.json()["connection"]["credentials_configured"] is True
    assert "operator" not in status.text
    assert "never-return-this-password" not in status.text
    assert tested.json()["qualification"]["device_count"] == 2
    assert tested.json()["qualification"]["capabilities"][0]["endpoint"] == "/dataservice/device"
    assert applied.status_code == 200
    assert applied.json()["saved"] is True
    assert "never-return-this-password" not in applied.text
    assert setup.tested is True
    assert setup.saved is True
    assert initial_client.closed is True
    assert setup.connected_client.closed is True
    assert refreshed_status.json()["connection"]["host"] == "new-vmanage.example.test"
    assert refreshed_status.json()["connection"]["port"] == 8443
    assert metadata.json()["connected"] is True


def test_setup_can_qualify_the_current_connection_without_credentials() -> None:
    with TestClient(create_web_app(client_factory=WebClient)) as browser:
        response = browser.post("/api/v1/setup/qualify")

    assert response.status_code == 200
    payload = response.json()
    assert payload["ready"] is True
    assert payload["qualification"]["device_count"] == 2
    assert payload["qualification"]["capabilities"][0]["id"] == "inventory"
    assert "username" not in response.text
    assert "password" not in response.text


def test_enterprise_setup_is_visible_but_connection_changes_are_denied() -> None:
    owner = GovernanceActor(
        id="oidc-owner",
        tenant_id="customer-a",
        display_name="Customer Owner",
        role="owner",
    )
    setup = BrowserSetupService()
    application = create_web_app(
        client_factory=WebClient,
        deployment_settings=_oidc_settings(),
        identity_verifier=StaticIdentityVerifier(owner),
        setup_service=setup,
    )
    headers = {"Authorization": "Bearer signed-token"}

    with TestClient(
        application,
        base_url="https://operations.example.com",
        headers=headers,
    ) as browser:
        status = browser.get("/api/v1/setup")
        denied = browser.post(
            "/api/v1/setup/test",
            json={
                "host": "vmanage.example.test",
                "port": 443,
                "username": "operator",
                "password": "secret",
                "verify_ssl": True,
            },
        )
        denied_apply = browser.post(
            "/api/v1/setup/apply",
            json={
                "host": "vmanage.example.test",
                "port": 443,
                "username": "operator",
                "password": "secret",
                "verify_ssl": True,
            },
        )

    assert status.status_code == 200
    assert status.json()["editable"] is False
    assert status.json()["connection"]["config_file"] == "deployment-managed"
    assert denied.status_code == 403
    assert denied_apply.status_code == 403
    assert setup.tested is False


def test_setup_api_rejects_invalid_connection_forms() -> None:
    application = create_web_app(
        client_factory=WebClient,
        setup_service=BrowserSetupService(),
    )

    with TestClient(application) as browser:
        invalid_test = browser.post(
            "/api/v1/setup/test",
            json={
                "host": "https://vmanage.example.test/path",
                "port": 443,
                "username": "operator",
                "password": "secret",
                "verify_ssl": True,
            },
        )
        invalid_apply = browser.post(
            "/api/v1/setup/apply",
            json={"host": "vmanage.example.test"},
        )

    assert invalid_test.status_code == 400
    assert invalid_apply.status_code == 400


def test_setup_api_reports_test_and_connection_failures_without_secrets() -> None:
    class FailingSetupService(BrowserSetupService):
        async def test(self, values: VManageSetupInput) -> EnvironmentQualification:
            del values
            raise RuntimeError("token=do-not-return")

        async def connect(
            self,
            values: VManageSetupInput,
        ) -> tuple[WebClient, EnvironmentQualification]:
            del values
            raise RuntimeError("password=do-not-return")

    application = create_web_app(
        client_factory=WebClient,
        setup_service=FailingSetupService(),
    )
    values = {
        "host": "vmanage.example.test",
        "port": 443,
        "username": "operator",
        "password": "secret",
        "verify_ssl": True,
    }

    with TestClient(application) as browser:
        tested = browser.post("/api/v1/setup/test", json=values)
        applied = browser.post("/api/v1/setup/apply", json=values)

    assert tested.status_code == 502
    assert applied.status_code == 502
    assert "do-not-return" not in tested.text
    assert "do-not-return" not in applied.text


def test_setup_apply_rejects_unready_environment_and_save_failure() -> None:
    class UnreadySetupService(BrowserSetupService):
        async def connect(
            self,
            values: VManageSetupInput,
        ) -> tuple[WebClient, EnvironmentQualification]:
            del values
            self.connected_client = WebClient()
            return self.connected_client, self._report().model_copy(update={"ready": False})

    class UnsavedSetupService(BrowserSetupService):
        def save(self, values: VManageSetupInput) -> None:
            del values
            raise SetupError("disk unavailable")

    values = {
        "host": "vmanage.example.test",
        "port": 443,
        "username": "operator",
        "password": "secret",
        "verify_ssl": True,
    }
    unready = UnreadySetupService()
    unsaved = UnsavedSetupService()

    with TestClient(create_web_app(client_factory=WebClient, setup_service=unready)) as browser:
        unready_response = browser.post("/api/v1/setup/apply", json=values)
    with TestClient(create_web_app(client_factory=WebClient, setup_service=unsaved)) as browser:
        unsaved_response = browser.post("/api/v1/setup/apply", json=values)

    assert unready_response.status_code == 409
    assert unready_response.json()["qualification"]["ready"] is False
    assert unready.connected_client is not None
    assert unready.connected_client.closed is True
    assert unsaved_response.status_code == 500
    assert unsaved.connected_client is not None
    assert unsaved.connected_client.closed is True


def test_enterprise_identity_owns_governed_resources_and_health_is_public(
    tmp_path,
    monkeypatch,
) -> None:
    actor = GovernanceActor(
        id="oidc-operator",
        tenant_id="customer-a",
        display_name="Customer Operator",
        role="operator",
    )
    state_dir = tmp_path / "browser-state"
    governance = GovernanceStore(state_dir)
    synchronize = Mock(wraps=governance.synchronize_authenticated_actor)
    monkeypatch.setattr(governance, "synchronize_authenticated_actor", synchronize)
    application = create_web_app(
        client_factory=WebClient,
        investigation_store=InvestigationStore(state_dir),
        governance_store=governance,
        workflow_store=WorkflowStore(state_dir),
        deployment_settings=_oidc_settings(),
        identity_verifier=StaticIdentityVerifier(actor),
    )
    headers = {"Authorization": "Bearer signed-token"}

    with TestClient(
        application,
        base_url="https://operations.example.com",
        headers=headers,
    ) as browser:
        metadata = browser.get("/api/v1/meta")
        created = browser.post("/api/v1/investigations", json={"title": "Customer incident"})
        collaboration = browser.get(
            f"/api/v1/collaborations/{created.json()['id']}"
        )
        draft = browser.post(
            "/api/v1/workflows/drafts",
            json={
                "investigation_id": created.json()["id"],
                "destination": "webex",
                "actor_id": "spoofed-actor",
            },
        )
        approved = browser.post(
            f"/api/v1/workflows/{draft.json()['id']}/approve",
            json={
                "actor_id": "spoofed-actor",
                "expected_hash": draft.json()["content_hash"],
            },
        )
        live = browser.get("/health/live", headers={"Authorization": ""})
        ready = browser.get("/health/ready", headers={"Authorization": ""})

    assert metadata.json()["identity_mode"] == "oidc"
    assert metadata.json()["actor"]["id"] == "oidc-operator"
    assert metadata.json()["actor"]["tenant_id"] == "customer-a"
    assert created.status_code == 201
    assert collaboration.json()["owner_id"] == "oidc-operator"
    assert draft.json()["audit_events"][0]["actor_id"] == "oidc-operator"
    assert approved.json()["approvals"][0]["actor_id"] == "oidc-operator"
    assert synchronize.call_count == 1
    assert live.json()["status"] == "live"
    assert ready.json()["status"] == "ready"


def test_enterprise_viewer_and_untrusted_host_are_rejected() -> None:
    viewer = GovernanceActor(
        id="oidc-viewer",
        tenant_id="customer-a",
        display_name="Customer Viewer",
        role="viewer",
    )
    application = create_web_app(
        client_factory=WebClient,
        deployment_settings=_oidc_settings(),
        identity_verifier=StaticIdentityVerifier(viewer),
    )
    headers = {"Authorization": "Bearer signed-token"}

    with TestClient(
        application,
        base_url="https://operations.example.com",
        headers=headers,
    ) as browser:
        metadata = browser.get("/api/v1/meta")
        mutation = browser.post("/api/v1/investigations", json={"title": "Denied"})
    with TestClient(
        application,
        base_url="https://untrusted.example.com",
        headers=headers,
    ) as browser:
        untrusted = browser.get("/api/v1/meta")

    assert metadata.status_code == 200
    assert mutation.status_code == 403
    assert untrusted.status_code == 400


def test_enterprise_operator_can_plan_but_cannot_approve_or_cancel(tmp_path) -> None:
    operator = GovernanceActor(
        id="oidc-operator",
        tenant_id="customer-a",
        display_name="Customer Operator",
        role="operator",
    )
    state_dir = tmp_path / "browser-state"
    snapshots = SnapshotStore(state_dir)
    before = _snapshot("before", datetime.now().astimezone())
    snapshots.add(before)
    application = create_web_app(
        client_factory=WebClient,
        snapshot_store=snapshots,
        change_plan_store=ChangePlanStore(state_dir),
        deployment_settings=_oidc_settings(),
        identity_verifier=StaticIdentityVerifier(operator),
    )
    headers = {"Authorization": "Bearer signed-token"}

    with TestClient(
        application,
        base_url="https://operations.example.com",
        headers=headers,
    ) as browser:
        created = browser.post(
            "/api/v1/change-plans",
            json={
                "operation": "attach-device-template",
                "intent": "Review canary template update",
                "target_id": "template-1",
                "targets": ["edge-1"],
                "canary_targets": ["edge-1"],
                "pre_snapshot_id": before.id,
            },
        )
        approved = browser.post(
            f"/api/v1/change-plans/{created.json()['id']}/approve",
            json={"expected_hash": created.json()["plan_hash"]},
        )
        cancelled = browser.post(
            f"/api/v1/change-plans/{created.json()['id']}/cancel"
        )

    assert created.status_code == 201
    assert approved.status_code == 403
    assert cancelled.status_code == 403


def test_browser_assets_expose_sessions_streaming_and_accessibility() -> None:
    with TestClient(create_web_app(client_factory=WebClient)) as browser:
        page = browser.get("/").text
        script = browser.get("/assets/app.js").text

    assert 'class="skip-link" href="#main-content"' in page
    assert '<main id="main-content"' in page
    assert 'aria-busy="false"' in page
    assert 'role="alert"' in page
    assert "<caption>Fabric devices by site and reachability</caption>" in page
    assert 'id="investigation-select"' in page
    assert 'id="export-button"' in page
    assert 'id="cancel-button"' in page
    assert 'id="assistant-mode-evidence"' in page
    assert 'id="assistant-mode-generative"' in page
    assert 'id="evidence-freshness"' in page
    assert 'data-view="topology"' in page
    assert 'data-view="actions"' in page
    assert 'data-view="integrations"' in page
    assert 'data-view="changes"' in page
    assert 'id="topology-map"' in page
    assert 'id="topology-context"' in page
    assert 'id="actions-list"' in page
    assert 'id="capture-snapshot-button"' in page
    assert 'id="include-configuration-hashes"' in page
    assert 'id="before-snapshot"' in page
    assert 'id="after-snapshot"' in page
    assert 'id="comparison-result"' in page
    assert 'id="trend-series"' in page
    assert 'id="connector-list"' in page
    assert 'id="connector-evidence"' in page
    assert 'id="workflow-destination"' in page
    assert 'id="workflow-list"' in page
    assert 'id="collaboration-owner"' in page
    assert 'id="collaboration-timeline"' in page
    assert 'id="collaboration-comment"' in page
    assert 'id="audience-view"' in page
    assert 'id="audience-summary"' in page
    assert 'id="collaboration-hypotheses"' in page
    assert 'id="hypothesis-form"' in page
    assert 'id="agent-list"' in page
    assert 'id="agent-form"' in page
    assert 'id="change-plan-form"' in page
    assert 'id="change-plan-list"' in page
    assert 'id="change-pre-snapshot"' in page
    assert 'id="change-post-snapshot"' in page
    assert 'data-view="setup"' in page
    assert 'id="setup-form"' in page
    assert 'id="setup-qualification"' in page
    assert 'id="setup-integration-list"' in page
    assert 'id="setup-download-report"' in page
    assert 'id="setup-copy-command"' in page
    assert "vmanage-mcp-install" in page
    assert "Apply unavailable" in script
    assert 'currentInvestigation' in script
    assert '"/api/v1/chat/stream"' in script
    assert "assistantMode" in script
    assert "function renderTopology" in script
    assert "function renderActions" in script
    assert "function renderComparison" in script
    assert "function renderTrends" in script
    assert "function renderConnectors" in script
    assert "connector.handoff_url" in script
    assert "function renderWorkflows" in script
    assert "function renderCollaboration" in script
    assert "function renderAudienceView" in script
    assert "function renderAgents" in script
    assert "function renderChangePlans" in script
    assert "function renderSetupQualification" in script
    assert "function testSetupConnection" in script
    assert "function downloadSetupQualification" in script
    assert "function copySetupInstallCommand" in script
    assert 'setAttribute("aria-busy"' in script
    assert "shouldStickToBottom" in script


def test_overview_returns_correlated_evidence() -> None:
    with TestClient(create_web_app(client_factory=WebClient)) as browser:
        response = browser.get("/api/overview")
        versioned_response = browser.get("/api/v1/overview")

    assert response.status_code == 200
    payload = response.json()
    assert payload["health"] == "critical"
    assert payload["devices"]["total"] == 2
    assert payload["devices"]["unreachable"] == 1
    assert payload["alarms"]["Critical"] == 1
    assert payload["impact"]["scope"] == "site"
    assert payload["sources"][0]["label"].startswith("GET /dataservice/")
    assert datetime.fromisoformat(payload["updated_at"])
    assert versioned_response.json()["health"] == payload["health"]


def test_connected_context_endpoints_return_versioned_evidence() -> None:
    with TestClient(create_web_app(client_factory=WebClient)) as browser:
        topology = browser.get("/api/v1/topology")
        actions = browser.get("/api/v1/actions")

    assert topology.status_code == 200
    graph = topology.json()
    assert graph["schema_version"] == 1
    assert graph["summary"]["sites"] == 1
    assert graph["summary"]["controllers"] == 1
    assert graph["summary"]["edges"] == 1
    assert graph["generated_at"]
    assert graph["sources"]
    assert actions.status_code == 200
    inbox = actions.json()
    assert inbox["schema_version"] == 1
    assert inbox["summary"]["P1"] == 1
    assert inbox["items"][0]["category"] == "control-plane"
    assert len(inbox["items"][0]["evidence"]) == 2


def test_assurance_snapshot_lifecycle_and_comparison(tmp_path) -> None:
    snapshot_store = SnapshotStore(tmp_path / "browser-state")
    application = create_web_app(client_factory=WebClient, snapshot_store=snapshot_store)

    with TestClient(application) as browser:
        first = browser.post(
            "/api/v1/snapshots",
            params={"include_configuration_hashes": "true"},
        )
        second = browser.post("/api/v1/snapshots")
        listed = browser.get("/api/v1/snapshots")
        loaded = browser.get(f"/api/v1/snapshots/{first.json()['id']}")
        comparison = browser.get(
            "/api/v1/comparisons",
            params={"before": first.json()["id"], "after": second.json()["id"]},
        )
        trends = browser.get("/api/v1/trends", params={"days": 30})
        freshness = browser.get(
            "/api/v1/freshness",
            params={"snapshot_id": second.json()["id"], "stale_after_seconds": 300},
        )
        deleted = browser.delete(f"/api/v1/snapshots/{first.json()['id']}")

    assert first.status_code == 201
    assert first.json()["schema_version"] == 1
    assert first.json()["devices"]
    assert first.json()["sla_samples"][0]["fault_domain"] == "internet"
    assert len(first.json()["configuration_hashes"]["10.0.0.1"]) == 64
    assert "never-return-this" not in first.text
    assert second.status_code == 201
    assert len(listed.json()["items"]) == 2
    assert loaded.json()["id"] == first.json()["id"]
    assert comparison.status_code == 200
    assert comparison.json()["verdict"] == "unchanged"
    assert trends.status_code == 200
    assert trends.json()["snapshot_count"] == 2
    assert {series["fault_domain"] for series in trends.json()["series"]} == {
        "branch",
        "internet",
    }
    assert freshness.status_code == 200
    assert freshness.json()["stale_after_seconds"] == 300
    assert deleted.status_code == 204


def test_assurance_api_rejects_invalid_comparisons_and_ranges(tmp_path) -> None:
    snapshot_store = SnapshotStore(tmp_path / "browser-state")
    application = create_web_app(client_factory=WebClient, snapshot_store=snapshot_store)

    with TestClient(application) as browser:
        missing_ids = browser.get("/api/v1/comparisons")
        unknown_ids = browser.get(
            "/api/v1/comparisons",
            params={"before": "missing", "after": "also-missing"},
        )
        invalid_days = browser.get("/api/v1/trends", params={"days": 0})
        non_numeric_days = browser.get("/api/v1/trends", params={"days": "thirty"})
        invalid_hash_flag = browser.post(
            "/api/v1/snapshots",
            params={"include_configuration_hashes": "sometimes"},
        )
        missing_snapshot = browser.get("/api/v1/snapshots/missing")
        missing_delete = browser.delete("/api/v1/snapshots/missing")
        invalid_freshness = browser.get(
            "/api/v1/freshness",
            params={"snapshot_id": "missing", "stale_after_seconds": 0},
        )

    assert missing_ids.status_code == 400
    assert unknown_ids.status_code == 404
    assert invalid_days.status_code == 400
    assert non_numeric_days.status_code == 400
    assert invalid_hash_flag.status_code == 400
    assert missing_snapshot.status_code == 404
    assert missing_delete.status_code == 404
    assert invalid_freshness.status_code == 400


def test_connector_and_preview_only_workflow_apis(tmp_path) -> None:
    class EvidenceConnector:
        descriptor = ConnectorDescriptor(
            id="external-test",
            display_name="External Test",
            kind="test",
            enabled=True,
            timeout_ms=100,
            data_classification="operational",
            capabilities=("alerts",),
            provenance="test://external",
        )

        async def collect(self, capability: str, context: dict):
            return [
                ConnectorEvidence(
                    id="external-alert-1",
                    connector_id="spoofed",
                    capability=capability,
                    title="External path alert",
                    data={"site_id": context["site_id"]},
                    provenance="spoofed://source",
                )
            ]

    registry = ConnectorRegistry()
    registry.register(EvidenceConnector())
    investigations = InvestigationStore(tmp_path / "browser-state")
    investigation = investigations.create("Site 100 incident")
    investigations.append_message(
        investigation.id,
        "assistant",
        "Site 100 has lost control connectivity.",
    )
    workflows = WorkflowStore(tmp_path / "browser-state")
    application = create_web_app(
        client_factory=WebClient,
        investigation_store=investigations,
        connector_registry=registry,
        workflow_store=workflows,
    )

    with TestClient(application) as browser:
        connectors = browser.get("/api/v1/connectors")
        evidence = browser.post(
            "/api/v1/connectors/collect",
            json={"capability": "alerts", "context": {"site_id": "100"}},
        )
        drafted = browser.post(
            "/api/v1/workflows/drafts",
            json={
                "investigation_id": investigation.id,
                "destination": "servicenow",
                "target": "Network Operations",
            },
        )
        approved = browser.post(
            f"/api/v1/workflows/{drafted.json()['id']}/approve",
            json={
                "actor_id": "local-operator",
                "expected_hash": drafted.json()["content_hash"],
            },
        )
        listed = browser.get("/api/v1/workflows")
        send = browser.post(f"/api/v1/workflows/{drafted.json()['id']}/send")

    assert connectors.json()["items"][0]["id"] == "external-test"
    assert evidence.status_code == 200
    assert evidence.json()["items"][0]["connector_id"] == "external-test"
    assert evidence.json()["items"][0]["provenance"] == "test://external"
    assert drafted.status_code == 201
    assert drafted.json()["delivery_enabled"] is False
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"
    assert len(listed.json()["items"]) == 1
    assert send.status_code == 404


def test_connector_and_workflow_apis_reject_invalid_requests(tmp_path) -> None:
    investigations = InvestigationStore(tmp_path / "browser-state")
    investigation = investigations.create("Validation")
    workflows = WorkflowStore(tmp_path / "browser-state")
    application = create_web_app(
        client_factory=WebClient,
        investigation_store=investigations,
        workflow_store=workflows,
    )

    with TestClient(application) as browser:
        invalid_capability = browser.post(
            "/api/v1/connectors/collect",
            json={"capability": "", "context": {}},
        )
        long_capability = browser.post(
            "/api/v1/connectors/collect",
            json={"capability": "x" * 101, "context": {}},
        )
        invalid_context = browser.post(
            "/api/v1/connectors/collect",
            json={"capability": "alerts", "context": []},
        )
        missing_investigation_id = browser.post(
            "/api/v1/workflows/drafts",
            json={"destination": "slack"},
        )
        invalid_destination = browser.post(
            "/api/v1/workflows/drafts",
            json={"investigation_id": investigation.id, "destination": "email"},
        )
        invalid_target = browser.post(
            "/api/v1/workflows/drafts",
            json={
                "investigation_id": investigation.id,
                "destination": "slack",
                "target": 42,
            },
        )
        invalid_actor = browser.post(
            "/api/v1/workflows/drafts",
            json={
                "investigation_id": investigation.id,
                "destination": "slack",
                "actor_id": "",
            },
        )
        invalid_ttl_type = browser.post(
            "/api/v1/workflows/drafts",
            json={
                "investigation_id": investigation.id,
                "destination": "slack",
                "ttl_minutes": True,
            },
        )
        invalid_ttl_range = browser.post(
            "/api/v1/workflows/drafts",
            json={
                "investigation_id": investigation.id,
                "destination": "slack",
                "ttl_minutes": 1,
            },
        )
        missing_investigation = browser.post(
            "/api/v1/workflows/drafts",
            json={"investigation_id": "missing", "destination": "slack"},
        )
        missing_workflow = browser.get("/api/v1/workflows/missing")

    assert invalid_capability.status_code == 400
    assert long_capability.status_code == 400
    assert invalid_context.status_code == 400
    assert missing_investigation_id.status_code == 400
    assert invalid_destination.status_code == 400
    assert invalid_target.status_code == 400
    assert invalid_actor.status_code == 400
    assert invalid_ttl_type.status_code == 400
    assert invalid_ttl_range.status_code == 400
    assert missing_investigation.status_code == 404
    assert missing_workflow.status_code == 404


def test_collaboration_and_agent_governance_apis(tmp_path) -> None:
    investigations = InvestigationStore(tmp_path / "browser-state")
    governance = GovernanceStore(
        tmp_path / "browser-state",
        bootstrap_owner=GovernanceActor(
            id="local-owner",
            tenant_id="local",
            display_name="Local owner",
            role="owner",
        ),
    )
    registry = ConnectorRegistry()

    class RestrictedConnector:
        descriptor = ConnectorDescriptor(
            id="restricted-source",
            display_name="Restricted Source",
            kind="test",
            enabled=True,
            timeout_ms=100,
            data_classification="restricted",
            capabilities=("alerts",),
            provenance="test://restricted",
        )

        async def collect(self, capability: str, context: dict):
            return [
                ConnectorEvidence(
                    id="alert-1",
                    connector_id="restricted-source",
                    capability=capability,
                    title="Restricted alert",
                    provenance="test://restricted",
                )
            ]

    registry.register(RestrictedConnector())
    application = create_web_app(
        client_factory=WebClient,
        investigation_store=investigations,
        connector_registry=registry,
        governance_store=governance,
    )

    with TestClient(application) as browser:
        metadata = browser.get("/api/v1/meta")
        created = browser.post("/api/v1/investigations", json={"title": "Collaboration"})
        investigation_id = created.json()["id"]
        collaboration = browser.get(f"/api/v1/collaborations/{investigation_id}")
        executive_view = browser.get(
            f"/api/v1/investigations/{investigation_id}/view",
            params={"audience": "executive"},
        )
        invalid_view = browser.get(
            f"/api/v1/investigations/{investigation_id}/view",
            params={"audience": "administrator"},
        )
        comment = browser.post(
            f"/api/v1/collaborations/{investigation_id}/comments",
            json={"body": "Checking control plane", "expected_revision": 1},
        )
        actors = browser.get("/api/v1/actors")
        assigned = browser.post(
            f"/api/v1/collaborations/{investigation_id}/owner",
            json={"owner_id": "local-owner", "expected_revision": 2},
        )
        first_hypothesis = browser.post(
            f"/api/v1/collaborations/{investigation_id}/hypotheses",
            json={
                "title": "ISP path impairment",
                "confidence": "medium",
                "expected_revision": 3,
            },
        )
        second_hypothesis = browser.post(
            f"/api/v1/collaborations/{investigation_id}/hypotheses",
            json={
                "title": "Branch router failure",
                "confidence": "low",
                "expected_revision": 4,
            },
        )
        updated_hypothesis = browser.patch(
            f"/api/v1/collaborations/{investigation_id}/hypotheses/"
            f"{first_hypothesis.json()['hypotheses'][0]['id']}",
            json={"confidence": "high", "expected_revision": 5},
        )
        conflict = browser.post(
            f"/api/v1/collaborations/{investigation_id}/comments",
            json={"body": "Stale comment", "expected_revision": 2},
        )
        agent = browser.post(
            "/api/v1/agents",
            json={
                "id": "assurance-agent",
                "display_name": "Assurance Agent",
                "human_owner_id": "local-owner",
                "allowed_connectors": ["restricted-source"],
                "maximum_data_classification": "restricted",
            },
        )
        agents = browser.get("/api/v1/agents")
        evidence = browser.post(
            "/api/v1/connectors/collect",
            json={
                "capability": "alerts",
                "agent_id": "assurance-agent",
                "context": {},
            },
        )

    assert metadata.json()["identity_mode"] == "local-owner"
    assert metadata.json()["actor"]["id"] == "local-owner"
    assert collaboration.json()["owner_id"] == "local-owner"
    assert executive_view.json()["audience"] == "executive"
    assert executive_view.json()["messages"] == []
    assert invalid_view.status_code == 400
    assert comment.json()["revision"] == 2
    assert actors.json()["items"][0]["id"] == "local-owner"
    assert assigned.json()["revision"] == 3
    assert len(second_hypothesis.json()["hypotheses"]) == 2
    assert updated_hypothesis.json()["hypotheses"][0]["confidence"] == "high"
    assert conflict.status_code == 409
    assert agent.status_code == 201
    assert agents.json()["items"][0]["human_owner_id"] == "local-owner"
    assert evidence.json()["items"][0]["connector_id"] == "restricted-source"


def test_guarded_change_plan_api_never_executes(tmp_path) -> None:
    snapshots = SnapshotStore(tmp_path / "browser-state")
    before = _snapshot("before", datetime.now().astimezone())
    after = _snapshot(
        "after",
        datetime.now().astimezone(),
        reachable=False,
        critical_alarms=1,
        bfd_down=1,
    )
    snapshots.add(before)
    snapshots.add(after)
    plans = ChangePlanStore(tmp_path / "browser-state")
    application = create_web_app(
        client_factory=WebClient,
        snapshot_store=snapshots,
        change_plan_store=plans,
    )

    with TestClient(application) as browser:
        metadata = browser.get("/api/v1/meta")
        created = browser.post(
            "/api/v1/change-plans",
            json={
                "operation": "attach-device-template",
                "intent": "Canary template update",
                "target_id": "template-1",
                "targets": ["edge-1"],
                "canary_targets": ["edge-1"],
                "pre_snapshot_id": before.id,
                "parameters": {"template_uuid": "template-1"},
                "rollback_strategy": "Restore template-previous.",
            },
        )
        approved = browser.post(
            f"/api/v1/change-plans/{created.json()['id']}/approve",
            json={"expected_hash": created.json()["plan_hash"]},
        )
        verified = browser.post(
            f"/api/v1/change-plans/{created.json()['id']}/verify",
            json={"post_snapshot_id": after.id},
        )
        listed = browser.get("/api/v1/change-plans")
        loaded = browser.get(f"/api/v1/change-plans/{created.json()['id']}")
        apply_response = browser.post(f"/api/v1/change-plans/{created.json()['id']}/apply")

    assert metadata.json()["change_execution_enabled"] is False
    assert created.status_code == 201
    assert created.json()["execution_enabled"] is False
    assert approved.json()["status"] == "approved"
    assert verified.json()["status"] == "verification-failed"
    assert verified.json()["verification"]["rollback_recommended"] is True
    assert listed.json()["items"][0]["id"] == created.json()["id"]
    assert loaded.json()["plan_hash"] == created.json()["plan_hash"]
    assert apply_response.status_code == 403
    assert "disabled" in apply_response.json()["error"].lower()


def test_change_plan_api_requires_existing_snapshot_and_valid_inputs(tmp_path) -> None:
    snapshots = SnapshotStore(tmp_path / "browser-state")
    plans = ChangePlanStore(tmp_path / "browser-state")
    application = create_web_app(
        client_factory=WebClient,
        snapshot_store=snapshots,
        change_plan_store=plans,
    )

    with TestClient(application) as browser:
        missing_snapshot = browser.post(
            "/api/v1/change-plans",
            json={
                "operation": "activate-central-policy",
                "intent": "Activate policy",
                "target_id": "policy-1",
                "targets": ["fabric"],
                "canary_targets": [],
                "pre_snapshot_id": "missing",
            },
        )
        invalid_operation = browser.post(
            "/api/v1/change-plans",
            json={
                "operation": "delete-policy",
                "intent": "Invalid",
                "target_id": "policy-1",
                "targets": ["fabric"],
                "pre_snapshot_id": "missing",
            },
        )
        missing_plan = browser.get("/api/v1/change-plans/missing")

    assert missing_snapshot.status_code == 404
    assert invalid_operation.status_code == 400
    assert missing_plan.status_code == 404


def test_change_plan_api_rejects_malformed_fields_and_invalid_transitions(tmp_path) -> None:
    snapshots = SnapshotStore(tmp_path / "browser-state")
    before = _snapshot("before", datetime.now().astimezone())
    snapshots.add(before)
    plans = ChangePlanStore(tmp_path / "browser-state")
    application = create_web_app(
        client_factory=WebClient,
        snapshot_store=snapshots,
        change_plan_store=plans,
    )
    base = {
        "operation": "attach-device-template",
        "intent": "Canary update",
        "target_id": "template-1",
        "targets": ["edge-1"],
        "canary_targets": ["edge-1"],
        "pre_snapshot_id": before.id,
    }

    with TestClient(application) as browser:
        invalid_identity = browser.post(
            "/api/v1/change-plans",
            json={**base, "intent": None},
        )
        invalid_targets = browser.post(
            "/api/v1/change-plans",
            json={**base, "targets": "edge-1"},
        )
        invalid_canaries = browser.post(
            "/api/v1/change-plans",
            json={**base, "canary_targets": [1]},
        )
        missing_pre_snapshot = browser.post(
            "/api/v1/change-plans",
            json={**base, "pre_snapshot_id": ""},
        )
        invalid_payloads = browser.post(
            "/api/v1/change-plans",
            json={**base, "parameters": [], "expected_outcomes": "healthy"},
        )
        invalid_outcome = browser.post(
            "/api/v1/change-plans",
            json={**base, "expected_outcomes": [1]},
        )
        invalid_rollback = browser.post(
            "/api/v1/change-plans",
            json={**base, "rollback_strategy": 42},
        )
        invalid_ttl = browser.post(
            "/api/v1/change-plans",
            json={**base, "ttl_minutes": True},
        )
        invalid_canary_scope = browser.post(
            "/api/v1/change-plans",
            json={**base, "canary_targets": ["edge-2"]},
        )
        created = browser.post("/api/v1/change-plans", json=base).json()
        missing_hash = browser.post(
            f"/api/v1/change-plans/{created['id']}/approve",
            json={},
        )
        invalid_approval_minutes = browser.post(
            f"/api/v1/change-plans/{created['id']}/approve",
            json={"expected_hash": created["plan_hash"], "approval_minutes": True},
        )
        missing_approval_plan = browser.post(
            "/api/v1/change-plans/missing/approve",
            json={"expected_hash": "hash"},
        )
        wrong_hash = browser.post(
            f"/api/v1/change-plans/{created['id']}/approve",
            json={"expected_hash": "wrong"},
        )
        missing_post_id = browser.post(
            f"/api/v1/change-plans/{created['id']}/verify",
            json={},
        )
        missing_verification_plan = browser.post(
            "/api/v1/change-plans/missing/verify",
            json={"post_snapshot_id": before.id},
        )
        missing_post_snapshot = browser.post(
            f"/api/v1/change-plans/{created['id']}/verify",
            json={"post_snapshot_id": "missing"},
        )
        unapproved_verification = browser.post(
            f"/api/v1/change-plans/{created['id']}/verify",
            json={"post_snapshot_id": before.id},
        )
        missing_cancel = browser.post("/api/v1/change-plans/missing/cancel")

    responses = (
        invalid_identity,
        invalid_targets,
        invalid_canaries,
        missing_pre_snapshot,
        invalid_payloads,
        invalid_outcome,
        invalid_rollback,
        invalid_ttl,
        invalid_canary_scope,
        missing_hash,
        invalid_approval_minutes,
    )
    assert all(response.status_code == 400 for response in responses)
    assert missing_approval_plan.status_code == 404
    assert wrong_hash.status_code == 409
    assert missing_post_id.status_code == 400
    assert missing_verification_plan.status_code == 404
    assert missing_post_snapshot.status_code == 404
    assert unapproved_verification.status_code == 409
    assert missing_cancel.status_code == 404


def test_workflow_approval_and_cancellation_conflicts(tmp_path) -> None:
    investigations = InvestigationStore(tmp_path / "browser-state")
    investigation = investigations.create("Approval validation")
    workflows = WorkflowStore(tmp_path / "browser-state")
    application = create_web_app(
        client_factory=WebClient,
        investigation_store=investigations,
        workflow_store=workflows,
    )

    with TestClient(application) as browser:
        draft = browser.post(
            "/api/v1/workflows/drafts",
            json={"investigation_id": investigation.id, "destination": "webex"},
        ).json()
        missing_actor = browser.post(
            f"/api/v1/workflows/{draft['id']}/approve",
            json={"expected_hash": draft["content_hash"]},
        )
        missing_hash = browser.post(
            f"/api/v1/workflows/{draft['id']}/approve",
            json={"actor_id": "operator"},
        )
        wrong_hash = browser.post(
            f"/api/v1/workflows/{draft['id']}/approve",
            json={"actor_id": "operator", "expected_hash": "wrong"},
        )
        missing_approve = browser.post(
            "/api/v1/workflows/missing/approve",
            json={"actor_id": "operator", "expected_hash": "hash"},
        )
        approved = browser.post(
            f"/api/v1/workflows/{draft['id']}/approve",
            json={"actor_id": "operator", "expected_hash": draft["content_hash"]},
        )
        cancel_approved = browser.post(
            f"/api/v1/workflows/{draft['id']}/cancel",
            json={"actor_id": "operator"},
        )
        invalid_cancel_actor = browser.post(
            "/api/v1/workflows/missing/cancel",
            json={"actor_id": ""},
        )
        missing_cancel = browser.post(
            "/api/v1/workflows/missing/cancel",
            json={"actor_id": "operator"},
        )

    assert missing_actor.status_code == 400
    assert missing_hash.status_code == 400
    assert wrong_hash.status_code == 409
    assert missing_approve.status_code == 404
    assert approved.status_code == 200
    assert cancel_approved.status_code == 409
    assert invalid_cancel_actor.status_code == 400
    assert missing_cancel.status_code == 404


def test_versioned_investigation_lifecycle(tmp_path) -> None:
    store = InvestigationStore(tmp_path / "browser-state")
    application = create_web_app(client_factory=WebClient, investigation_store=store)

    with TestClient(application) as browser:
        created = browser.post(
            "/api/v1/investigations",
            json={"title": "Site 100 incident"},
        )
        investigation_id = created.json()["id"]
        chat = browser.post(
            "/api/v1/chat",
            json={"message": "Show unreachable devices", "investigation_id": investigation_id},
        )
        pinned = browser.post(
            f"/api/v1/investigations/{investigation_id}/pins",
            json={
                "id": "device:10.0.0.1",
                "type": "device",
                "label": "edge-1",
                "data": {"system_ip": "10.0.0.1", "password": "not-exported"},
            },
        )
        loaded = browser.get(f"/api/v1/investigations/{investigation_id}")
        listed = browser.get("/api/v1/investigations")
        markdown = browser.get(
            f"/api/v1/investigations/{investigation_id}/export?format=markdown"
        )
        exported_json = browser.get(
            f"/api/v1/investigations/{investigation_id}/export?format=json"
        )
        unpinned = browser.delete(
            f"/api/v1/investigations/{investigation_id}/pins/device%3A10.0.0.1"
        )
        deleted = browser.delete(f"/api/v1/investigations/{investigation_id}")
        missing = browser.get(f"/api/v1/investigations/{investigation_id}")

    assert created.status_code == 201
    assert chat.status_code == 200
    assert chat.json()["investigation_id"] == investigation_id
    assert pinned.status_code == 200
    assert [message["role"] for message in loaded.json()["messages"]] == [
        "user",
        "assistant",
    ]
    assert listed.json()["items"][0]["message_count"] == 2
    assert markdown.headers["content-type"].startswith("text/markdown")
    assert "Site 100 incident" in markdown.text
    assert "not-exported" not in markdown.text
    assert exported_json.headers["content-type"].startswith("application/json")
    assert exported_json.json()["schema_version"] == 1
    assert unpinned.status_code == 204
    assert deleted.status_code == 204
    assert missing.status_code == 404


def test_investigation_api_rejects_invalid_inputs(tmp_path) -> None:
    store = InvestigationStore(tmp_path / "browser-state")
    application = create_web_app(client_factory=WebClient, investigation_store=store)

    with TestClient(application) as browser:
        blank_title = browser.post("/api/v1/investigations", json={"title": "   "})
        missing_chat = browser.post(
            "/api/v1/chat",
            json={"message": "Assess fabric health", "investigation_id": "missing"},
        )
        invalid_pin = browser.post(
            "/api/v1/investigations/missing/pins",
            json={"id": "config:1", "type": "configuration", "label": "Raw config"},
        )
        invalid_export = browser.get(
            "/api/v1/investigations/missing/export?format=xml"
        )

    assert blank_title.status_code == 400
    assert missing_chat.status_code == 404
    assert invalid_pin.status_code in {400, 404}
    assert invalid_export.status_code == 400


def test_investigation_api_handles_malformed_and_missing_resources(tmp_path) -> None:
    store = InvestigationStore(tmp_path / "browser-state")
    investigation = store.create("Validation")
    application = create_web_app(client_factory=WebClient, investigation_store=store)

    with TestClient(application) as browser:
        malformed = browser.post(
            "/api/v1/investigations",
            content="not-json",
            headers={"content-type": "application/json"},
        )
        non_object = browser.post("/api/v1/investigations", json=[])
        long_title = browser.post("/api/v1/investigations", json={"title": "x" * 201})
        invalid_pin = browser.post(
            f"/api/v1/investigations/{investigation.id}/pins",
            json={"id": "configuration:1", "type": "configuration", "label": "Config"},
        )
        missing_delete = browser.delete("/api/v1/investigations/missing")
        missing_unpin = browser.delete(
            "/api/v1/investigations/missing/pins/source%3Amissing"
        )
        absent_pin = browser.delete(
            f"/api/v1/investigations/{investigation.id}/pins/source%3Amissing"
        )

    assert malformed.status_code == 400
    assert non_object.status_code == 400
    assert long_title.status_code == 400
    assert invalid_pin.status_code == 400
    assert missing_delete.status_code == 404
    assert missing_unpin.status_code == 404
    assert absent_pin.status_code == 404


def test_default_investigation_store_uses_configured_private_directory(
    tmp_path,
    monkeypatch,
) -> None:
    state_dir = tmp_path / "configured-state"
    monkeypatch.setenv("VMANAGE_WEB_STATE_DIR", str(state_dir))

    with TestClient(create_web_app(client_factory=WebClient)) as browser:
        response = browser.get("/api/v1/investigations")
        snapshots = browser.get("/api/v1/snapshots")
        workflows = browser.get("/api/v1/workflows")
        change_plans = browser.get("/api/v1/change-plans")

    assert response.status_code == 200
    assert response.json() == {"items": []}
    assert snapshots.json() == {"items": []}
    assert workflows.json() == {"items": []}
    assert change_plans.json() == {"items": []}
    assert state_dir.stat().st_mode & 0o777 == 0o700


def test_streamed_chat_reports_bounded_phases_and_persists(tmp_path) -> None:
    store = InvestigationStore(tmp_path / "browser-state")
    investigation = store.create("Stream test")
    application = create_web_app(client_factory=WebClient, investigation_store=store)

    with TestClient(application) as browser:
        response = browser.post(
            "/api/v1/chat/stream",
            json={
                "message": "Assess fabric health",
                "investigation_id": investigation.id,
            },
        )

    events = _sse_events(response)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert [event["phase"] for event in events] == [
        "accepted",
        "collecting",
        "correlating",
        "completed",
    ]
    assert events[-1]["result"]["kind"] == "health"
    assert "data" not in events[1]
    loaded = store.get(investigation.id)
    assert loaded is not None
    assert len(loaded.messages) == 2


def test_streamed_chat_can_add_server_side_provider_explanation(tmp_path) -> None:
    class Explainer:
        provider_name = "test-provider"

        def __init__(self) -> None:
            self.received = ""

        def explain(self, question: str, result: dict) -> str:
            self.received = json.dumps(result)
            return f"Provider explanation for: {question}"

    store = InvestigationStore(tmp_path / "browser-state")
    investigation = store.create("Provider test")
    explainer = Explainer()
    application = create_web_app(
        client_factory=WebClient,
        investigation_store=store,
        browser_explainer=explainer,
    )

    with TestClient(application) as browser:
        metadata = browser.get("/api/v1/meta")
        response = browser.post(
            "/api/v1/chat/stream",
            json={
                "message": "Assess fabric health",
                "investigation_id": investigation.id,
                "assistant_mode": "generative",
            },
        )

    events = _sse_events(response)
    assert metadata.json()["assistant_modes"] == ["evidence", "generative"]
    assert metadata.json()["browser_ai_provider"] == "test-provider"
    assert [event["phase"] for event in events] == [
        "accepted",
        "collecting",
        "correlating",
        "explaining",
        "completed",
    ]
    result = events[-1]["result"]
    assert result["answer"].startswith("Provider explanation")
    assert result["deterministic_answer"].startswith("Fabric health is")
    assert result["assistant_mode"] == "generative"
    assert "password" not in explainer.received.lower()


def test_browser_ai_modes_validate_and_fall_back_to_evidence(tmp_path, monkeypatch) -> None:
    class FailingExplainer:
        provider_name = "failing-provider"

        def explain(self, question: str, result: dict) -> str:
            raise RuntimeError("provider unavailable")

    monkeypatch.delenv("BROWSER_AI_PROVIDER", raising=False)
    with TestClient(create_web_app(client_factory=WebClient)) as browser:
        invalid_mode = browser.post(
            "/api/v1/chat",
            json={"message": "Assess fabric health", "assistant_mode": "automatic"},
        )
        unavailable = browser.post(
            "/api/v1/chat",
            json={"message": "Assess fabric health", "assistant_mode": "generative"},
        )

    application = create_web_app(
        client_factory=WebClient,
        browser_explainer=FailingExplainer(),
    )
    with TestClient(application) as browser:
        fallback = browser.post(
            "/api/v1/chat",
            json={"message": "Assess fabric health", "assistant_mode": "generative"},
        )

    assert invalid_mode.status_code == 400
    assert unavailable.status_code == 409
    assert fallback.status_code == 200
    assert fallback.json()["assistant_mode"] == "evidence"
    assert fallback.json()["explanation_error"] == "Provider explanation unavailable"


def test_streamed_chat_redacts_and_persists_errors(tmp_path, monkeypatch) -> None:
    async def fail_with_secret(_assistant, _message):
        raise RuntimeError("token=super-secret")

    store = InvestigationStore(tmp_path / "browser-state")
    investigation = store.create("Failure test")
    monkeypatch.setattr(EvidenceAssistant, "ask", fail_with_secret)
    application = create_web_app(client_factory=WebClient, investigation_store=store)

    with TestClient(application) as browser:
        response = browser.post(
            "/api/v1/chat/stream",
            json={"message": "Assess fabric health", "investigation_id": investigation.id},
        )

    events = _sse_events(response)
    loaded = store.get(investigation.id)
    assert response.status_code == 200
    assert events[-1]["phase"] == "error"
    assert "super-secret" not in response.text
    assert loaded is not None
    assert loaded.messages[-1].role == "system"
    assert "super-secret" not in loaded.messages[-1].content


@pytest.mark.asyncio
async def test_assistant_stream_propagates_cancellation() -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class SlowClient(WebClient):
        async def get(self, endpoint: str, params=None) -> dict:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            raise AssertionError("unreachable")

    stream = EvidenceAssistant(SlowClient()).stream("Assess fabric health")
    assert (await anext(stream))["phase"] == "accepted"
    assert (await anext(stream))["phase"] == "collecting"
    assert (await anext(stream))["phase"] == "correlating"

    pending = asyncio.ensure_future(anext(stream))
    await started.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending

    assert cancelled.is_set()


def test_chat_routes_health_devices_alarms_and_diagnosis() -> None:
    with TestClient(create_web_app(client_factory=WebClient)) as browser:
        health = browser.post("/api/chat", json={"message": "Assess fabric health"})
        devices = browser.post("/api/chat", json={"message": "Show unreachable devices"})
        alarms = browser.post("/api/chat", json={"message": "Show critical alarms"})
        diagnosis = browser.post("/api/chat", json={"message": "Diagnose 10.0.0.1"})
        change = browser.post("/api/chat", json={"message": "Is it safe to make a change?"})

    assert health.status_code == 200
    assert health.json()["kind"] == "health"
    assert health.json()["evidence"]
    assert devices.json()["kind"] == "devices"
    assert devices.json()["data"]["count"] == 1
    assert alarms.json()["kind"] == "alarms"
    assert alarms.json()["data"]["count"] == 1
    assert diagnosis.json()["kind"] == "diagnosis"
    assert diagnosis.json()["data"]["device"]["hostname"] == "edge-1"
    assert change.json()["kind"] == "change-readiness"
    assert change.json()["status"] == "no-go"


def test_chat_rejects_empty_or_invalid_json() -> None:
    with TestClient(create_web_app(client_factory=WebClient)) as browser:
        empty = browser.post("/api/chat", json={"message": "   "})
        oversized_message = browser.post("/api/chat", json={"message": "x" * 2_001})
        invalid = browser.post(
            "/api/chat",
            content="not-json",
            headers={"content-type": "application/json"},
        )
        invalid_length = browser.post(
            "/api/chat",
            content="{}",
            headers={"content-type": "application/json", "content-length": "invalid"},
        )
        array_body = browser.post(
            "/api/chat",
            content="[]",
            headers={"content-type": "application/json"},
        )
        oversized_body = browser.post(
            "/api/v1/investigations",
            content=b'{"title":"' + (b"x" * webapp.MAX_REQUEST_BODY_BYTES) + b'"}',
            headers={"content-type": "application/json"},
        )

    assert empty.status_code == 400
    assert oversized_message.status_code == 400
    assert "2000" in oversized_message.json()["error"]
    assert invalid.status_code == 400
    assert invalid_length.status_code == 400
    assert array_body.status_code == 400
    assert oversized_body.status_code == 413


def test_default_web_app_loads_lazily(monkeypatch) -> None:
    monkeypatch.setenv("VMANAGE_WEB_AUTH_MODE", "local")
    monkeypatch.setenv("VMANAGE_WEB_HOST", "127.0.0.1")
    lazy_application = webapp._LazyWebApplication()

    with TestClient(lazy_application) as browser:
        response = browser.get("/health/live")

    assert response.json() == {"status": "live"}
    assert lazy_application._application is not None


def test_default_web_app_loader_reports_invalid_environment(monkeypatch) -> None:
    monkeypatch.setenv("VMANAGE_WEB_AUTH_MODE", "oidc")
    monkeypatch.setenv("VMANAGE_WEB_HOST", "0.0.0.0")
    monkeypatch.delenv("VMANAGE_WEB_PUBLIC_URL", raising=False)

    with pytest.raises(RuntimeError, match="Invalid browser deployment configuration"):
        webapp._load_default_web_app()


def test_browser_is_loopback_only(monkeypatch) -> None:
    runner = Mock()
    monkeypatch.setattr(webapp.uvicorn, "run", runner)

    webapp.main(["--host", "127.0.0.1", "--port", "9000"])

    runner.assert_called_once()
    assert runner.call_args.kwargs["host"] == "127.0.0.1"
    assert runner.call_args.kwargs["port"] == 9000
    assert runner.call_args.kwargs["proxy_headers"] is False

    with pytest.raises(SystemExit, match="local-only"):
        webapp.main(["--host", "0.0.0.0"])


def test_browser_main_reports_invalid_enterprise_configuration(monkeypatch) -> None:
    monkeypatch.setenv("VMANAGE_WEB_AUTH_MODE", "oidc")
    monkeypatch.setenv("VMANAGE_WEB_HOST", "0.0.0.0")
    monkeypatch.delenv("VMANAGE_WEB_PUBLIC_URL", raising=False)

    with pytest.raises(SystemExit, match="Invalid browser deployment configuration"):
        webapp.main([])


def test_unavailable_vmanage_keeps_workspace_accessible() -> None:
    def unavailable():
        raise RuntimeError("configuration unavailable")

    with TestClient(create_web_app(client_factory=unavailable)) as browser:
        page = browser.get("/")
        metadata = browser.get("/api/meta")
        setup = browser.get("/api/v1/setup")
        qualification = browser.post("/api/v1/setup/qualify")
        overview = browser.get("/api/overview")

    assert page.status_code == 200
    assert metadata.json()["connected"] is False
    assert metadata.json()["setup_required"] is True
    assert setup.json()["complete"] is False
    assert qualification.status_code == 503
    assert overview.status_code == 503
