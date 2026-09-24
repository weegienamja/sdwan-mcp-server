"""Static contracts for the production container and Kubernetes base."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
KUBERNETES_BASE = ROOT / "deploy" / "kubernetes" / "base"


def _manifest(name: str) -> dict:
    return yaml.safe_load((KUBERNETES_BASE / name).read_text(encoding="utf-8"))


def test_container_runs_non_root_through_validated_entrypoint() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "USER 10001:10001" in dockerfile
    assert 'ENTRYPOINT ["vmanage-web"]' in dockerfile
    assert 'CMD ["--host", "0.0.0.0", "--port", "8765"]' in dockerfile
    assert "/health/live" in dockerfile
    assert "VMANAGE_PASSWORD" not in dockerfile


def test_kubernetes_base_is_complete_and_references_existing_resources() -> None:
    kustomization = _manifest("kustomization.yaml")
    resources = kustomization["resources"]

    assert set(resources) == {
        "configmap.yaml",
        "persistent-volume-claim.yaml",
        "deployment.yaml",
        "service.yaml",
    }
    assert all((KUBERNETES_BASE / resource).is_file() for resource in resources)
    assert {_manifest(resource)["kind"] for resource in resources} == {
        "ConfigMap",
        "PersistentVolumeClaim",
        "Deployment",
        "Service",
    }


def test_kubernetes_workload_has_production_security_and_state_controls() -> None:
    deployment = _manifest("deployment.yaml")["spec"]
    pod = deployment["template"]["spec"]
    container = pod["containers"][0]

    assert deployment["replicas"] == 1
    assert deployment["strategy"]["type"] == "Recreate"
    assert pod["automountServiceAccountToken"] is False
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"
    assert container["securityContext"]["allowPrivilegeEscalation"] is False
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["securityContext"]["capabilities"]["drop"] == ["ALL"]
    assert container["livenessProbe"]["httpGet"]["path"] == "/health/live"
    assert container["readinessProbe"]["httpGet"]["path"] == "/health/ready"
    assert any(
        mount["mountPath"] == "/var/lib/cisco-vmanage-mcp"
        for mount in container["volumeMounts"]
    )


def test_kubernetes_config_is_oidc_first_and_contains_no_secrets() -> None:
    data = _manifest("configmap.yaml")["data"]

    assert data["VMANAGE_WEB_AUTH_MODE"] == "oidc"
    assert data["VMANAGE_VERIFY_SSL"] == "true"
    assert data["VMANAGE_MCP_TELEMETRY"] == "false"
    assert data["VMANAGE_MCP_IDE_TELEMETRY"] == "false"
    assert not any(
        term in key.lower()
        for key in data
        for term in ("password", "secret", "token", "api_key")
    )