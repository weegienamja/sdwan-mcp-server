"""Unit tests for health check computation and correlation logic.

Tests health signals, root-cause analysis, and failure handling using
mocked vManage API responses. No live API calls needed.
"""

import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from cisco_vmanage_mcp.services.health_check import (
    DataFetchResult,
    DataSource,
    DeviceHealth,
    FabricHealthReport,
    HealthLevel,
    HealthSignal,
    assess_fabric_health,
    assess_device_health,
    compute_alarm_signals,
    compute_device_health,
    _parse_bfd_count,
    _parse_control_count,
)
from cisco_vmanage_mcp.services.correlation import (
    CorrelationReport,
    ImpactAssessment,
    RootCauseHypothesis,
    SiteStatus,
    _analyze_failure_scope,
    _generate_root_causes,
    _group_by_site,
    correlate_fabric_state,
    diagnose_device,
)


# --- Test fixtures: realistic vManage API response data ---

def _make_device(
    hostname: str,
    system_ip: str,
    site_id: str = "100",
    device_type: str = "vedge",
    device_model: str = "vedge-C8000V",
    reachability: str = "reachable",
    bfd: int | str = 8,
    control: int | str = 3,
    state: str = "green",
) -> dict:
    """Create a device dict matching vManage /dataservice/device format."""
    return {
        "host-name": hostname,
        "system-ip": system_ip,
        "deviceId": system_ip,
        "site-id": site_id,
        "device-type": device_type,
        "device-model": device_model,
        "reachability": reachability,
        "bfdSessions": bfd,
        "controlConnections": control,
        "state": state,
        "version": "20.10.1",
        "uuid": f"UUID-{hostname}",
        "board-serial": f"SN-{hostname}",
    }


HEALTHY_FABRIC = [
    _make_device("vmanage", "10.10.1.1", "101", "vmanage", "vmanage", "reachable", "--", 5),
    _make_device("vsmart", "10.10.1.5", "101", "vsmart", "vsmart", "reachable", "--", 9),
    _make_device("vbond", "10.10.1.3", "101", "vbond", "vbond", "reachable", "--", "--"),
    _make_device("site1-cedge01", "10.10.1.13", "1001", bfd=8, control=3),
    _make_device("site2-cedge01", "10.10.1.15", "1002", bfd=8, control=3),
    _make_device("site3-vedge01", "10.10.1.17", "1003", bfd=8, control=3),
]

ONE_EDGE_DOWN = [
    _make_device("vmanage", "10.10.1.1", "101", "vmanage", "vmanage", "reachable", "--", 5),
    _make_device("vsmart", "10.10.1.5", "101", "vsmart", "vsmart", "reachable", "--", 9),
    _make_device("vbond", "10.10.1.3", "101", "vbond", "vbond", "reachable", "--", "--"),
    _make_device("site1-cedge01", "10.10.1.13", "1001", bfd=8, control=3),
    _make_device("site2-cedge01", "10.10.1.15", "1002", bfd=8, control=3),
    _make_device("dc-cedge01", "10.10.1.11", "100", reachability="unreachable", bfd=0, control=0, state="red"),
]

SITE_DOWN = [
    _make_device("vmanage", "10.10.1.1", "101", "vmanage", "vmanage", "reachable", "--", 5),
    _make_device("vsmart", "10.10.1.5", "101", "vsmart", "vsmart", "reachable", "--", 9),
    _make_device("site1-cedge01", "10.10.1.13", "200", reachability="unreachable", bfd=0, control=0, state="red"),
    _make_device("site1-cedge02", "10.10.1.14", "200", reachability="unreachable", bfd=0, control=0, state="red"),
    _make_device("site2-cedge01", "10.10.1.15", "1002", bfd=8, control=3),
]

CONTROLLER_DOWN = [
    _make_device("vmanage", "10.10.1.1", "101", "vmanage", "vmanage", "unreachable", "--", 0, "red"),
    _make_device("vsmart", "10.10.1.5", "101", "vsmart", "vsmart", "reachable", "--", 9),
    _make_device("site1-cedge01", "10.10.1.13", "1001", bfd=8, control=3),
]

NO_ALARMS = [{"severity": "Critical", "count": 0}, {"severity": "Major", "count": 0}]
CRITICAL_ALARMS = [{"severity": "Critical", "count": 3}, {"severity": "Major", "count": 1}]


# --- Tests for health_check.py ---

class TestParseHelpers:
    def test_parse_bfd_count_int(self):
        assert _parse_bfd_count(8) == 8

    def test_parse_bfd_count_string(self):
        assert _parse_bfd_count("8") == 8

    def test_parse_bfd_count_dash(self):
        assert _parse_bfd_count("--") == 0

    def test_parse_bfd_count_none(self):
        assert _parse_bfd_count(None) == 0

    def test_parse_control_count_normal(self):
        assert _parse_control_count(3) == 3

    def test_parse_control_count_dash(self):
        assert _parse_control_count("--") == 0


class TestComputeDeviceHealth:
    def test_healthy_wan_edge(self):
        raw = _make_device("site1", "10.0.0.1", bfd=8, control=3)
        dh = compute_device_health(raw)
        assert dh.overall_health == HealthLevel.HEALTHY
        assert len(dh.signals) == 0

    def test_unreachable_device_is_critical(self):
        raw = _make_device("dc-cedge01", "10.0.0.1", reachability="unreachable", bfd=0, control=0)
        dh = compute_device_health(raw)
        assert dh.overall_health == HealthLevel.CRITICAL
        assert any(s.level == HealthLevel.CRITICAL for s in dh.signals)
        assert dh.reachable is False

    def test_reachable_but_no_bfd(self):
        raw = _make_device("edge1", "10.0.0.1", bfd=0, control=3)
        dh = compute_device_health(raw)
        assert dh.overall_health == HealthLevel.DEGRADED
        assert any("0 BFD" in s.summary for s in dh.signals)

    def test_reachable_but_no_control(self):
        raw = _make_device("edge1", "10.0.0.1", bfd=8, control=0)
        dh = compute_device_health(raw)
        assert dh.overall_health == HealthLevel.DEGRADED
        assert any("0 control" in s.summary for s in dh.signals)

    def test_red_state_is_critical(self):
        raw = _make_device("edge1", "10.0.0.1", state="red")
        dh = compute_device_health(raw)
        assert any(s.level == HealthLevel.CRITICAL and "red" in s.summary for s in dh.signals)

    def test_yellow_state_is_degraded(self):
        raw = _make_device("edge1", "10.0.0.1", state="yellow")
        dh = compute_device_health(raw)
        assert any(s.level == HealthLevel.DEGRADED and "yellow" in s.summary for s in dh.signals)

    def test_controller_not_flagged_for_bfd(self):
        """Controllers don't have BFD sessions -- shouldn't be flagged."""
        raw = _make_device("vmanage", "10.0.0.1", device_type="vmanage", bfd="--", control=5)
        dh = compute_device_health(raw)
        assert not any("BFD" in s.summary for s in dh.signals)

    def test_signals_cite_data_source(self):
        raw = _make_device("edge1", "10.0.0.1", reachability="unreachable", bfd=0, control=0)
        dh = compute_device_health(raw)
        for s in dh.signals:
            assert s.source == DataSource.DEVICE_LIST


class TestComputeAlarmSignals:
    def test_no_alarms(self):
        signals = compute_alarm_signals({"Critical": 0, "Major": 0})
        assert len(signals) == 0

    def test_critical_alarms(self):
        signals = compute_alarm_signals({"Critical": 3, "Major": 0})
        assert len(signals) == 1
        assert signals[0].level == HealthLevel.CRITICAL

    def test_major_alarms_only(self):
        signals = compute_alarm_signals({"Critical": 0, "Major": 5})
        assert len(signals) == 1
        assert signals[0].level == HealthLevel.DEGRADED

    def test_critical_takes_precedence(self):
        signals = compute_alarm_signals({"Critical": 1, "Major": 10})
        assert signals[0].level == HealthLevel.CRITICAL


# --- Tests for correlation.py ---

class TestGroupBySite:
    def test_groups_correctly(self):
        devices = [
            compute_device_health(d) for d in [
                _make_device("a", "1.1.1.1", "100"),
                _make_device("b", "1.1.1.2", "100"),
                _make_device("c", "1.1.1.3", "200"),
            ]
        ]
        sites = _group_by_site(devices)
        assert len(sites) == 2
        assert len(sites["100"].devices) == 2
        assert len(sites["200"].devices) == 1

    def test_all_unreachable_site(self):
        devices = [
            compute_device_health(d) for d in [
                _make_device("a", "1.1.1.1", "200", reachability="unreachable", bfd=0, control=0),
                _make_device("b", "1.1.1.2", "200", reachability="unreachable", bfd=0, control=0),
            ]
        ]
        sites = _group_by_site(devices)
        assert sites["200"].all_unreachable is True


class TestAnalyzeFailureScope:
    def test_no_failures(self):
        devices = [compute_device_health(d) for d in HEALTHY_FABRIC]
        sites = _group_by_site(devices)
        impact = _analyze_failure_scope(devices, sites)
        assert impact.scope == "none"

    def test_single_device_failure_at_sole_site(self):
        """dc-cedge01 is the only device at site 100, so failure = site-level."""
        devices = [compute_device_health(d) for d in ONE_EDGE_DOWN]
        sites = _group_by_site(devices)
        impact = _analyze_failure_scope(devices, sites)
        assert impact.scope == "site"
        assert "100" in impact.affected_sites

    def test_single_device_failure_with_healthy_peer(self):
        """When one device fails but another at the same site is healthy => device scope."""
        fabric = [
            _make_device("vmanage", "10.1.1.1", "101", "vmanage", "vmanage"),
            _make_device("healthy-edge", "10.1.1.2", "100", bfd=8, control=3),
            _make_device("down-edge", "10.1.1.3", "100", reachability="unreachable", bfd=0, control=0, state="red"),
        ]
        devices = [compute_device_health(d) for d in fabric]
        sites = _group_by_site(devices)
        impact = _analyze_failure_scope(devices, sites)
        assert impact.scope == "device"
        assert "down-edge" in impact.affected_devices

    def test_site_failure(self):
        devices = [compute_device_health(d) for d in SITE_DOWN]
        sites = _group_by_site(devices)
        impact = _analyze_failure_scope(devices, sites)
        assert impact.scope == "site"
        assert "200" in impact.affected_sites

    def test_controller_failure_is_fabric_wide(self):
        devices = [compute_device_health(d) for d in CONTROLLER_DOWN]
        sites = _group_by_site(devices)
        impact = _analyze_failure_scope(devices, sites)
        assert impact.scope == "fabric-wide"
        assert "vmanage" in impact.affected_devices


class TestGenerateRootCauses:
    def test_isolated_device_down(self):
        devices = [compute_device_health(d) for d in ONE_EDGE_DOWN]
        sites = _group_by_site(devices)
        impact = ImpactAssessment(
            scope="device",
            affected_devices=["dc-cedge01"],
            affected_sites=["100"],
        )
        causes = _generate_root_causes(devices, sites, impact)
        assert len(causes) >= 1
        assert any("dc-cedge01" in c.hypothesis for c in causes)
        assert causes[0].confidence in ("high", "medium", "low")
        assert len(causes[0].suggested_checks) > 0

    def test_site_down(self):
        devices = [compute_device_health(d) for d in SITE_DOWN]
        sites = _group_by_site(devices)
        impact = ImpactAssessment(scope="site", affected_sites=["200"])
        causes = _generate_root_causes(devices, sites, impact)
        assert any("site 200" in c.hypothesis.lower() or "200" in c.hypothesis for c in causes)

    def test_controller_down(self):
        devices = [compute_device_health(d) for d in CONTROLLER_DOWN]
        sites = _group_by_site(devices)
        impact = ImpactAssessment(scope="fabric-wide", affected_devices=["vmanage"])
        causes = _generate_root_causes(devices, sites, impact)
        assert any("controller" in c.hypothesis.lower() for c in causes)

    def test_no_causes_when_healthy(self):
        devices = [compute_device_health(d) for d in HEALTHY_FABRIC]
        sites = _group_by_site(devices)
        impact = ImpactAssessment(scope="none")
        causes = _generate_root_causes(devices, sites, impact)
        assert len(causes) == 0


# --- Integration tests with mocked client ---

class TestAssessFabricHealth:
    @pytest.mark.asyncio
    async def test_healthy_fabric(self):
        client = AsyncMock()
        client.get = AsyncMock(side_effect=[
            {"data": HEALTHY_FABRIC},
            {"data": NO_ALARMS},
        ])

        report = await assess_fabric_health(client)
        assert report.overall_health == HealthLevel.HEALTHY
        assert not report.partial
        assert len(report.devices) == 6

    @pytest.mark.asyncio
    async def test_fabric_with_unreachable_device(self):
        client = AsyncMock()
        client.get = AsyncMock(side_effect=[
            {"data": ONE_EDGE_DOWN},
            {"data": NO_ALARMS},
        ])

        report = await assess_fabric_health(client)
        assert report.overall_health == HealthLevel.CRITICAL
        unreachable = [d for d in report.devices if not d.reachable]
        assert len(unreachable) == 1
        assert unreachable[0].hostname == "dc-cedge01"

    @pytest.mark.asyncio
    async def test_partial_result_on_alarm_timeout(self):
        """When alarm fetch times out, report should be partial but still return device data."""
        async def mock_get(endpoint, params=None):
            if "alarm" in endpoint:
                raise asyncio.TimeoutError()
            return {"data": HEALTHY_FABRIC}

        client = AsyncMock()
        client.get = AsyncMock(side_effect=mock_get)

        report = await assess_fabric_health(client)
        assert report.partial is True
        assert len(report.incomplete_sources) > 0
        # Device data should still be populated
        assert len(report.devices) == 6

    @pytest.mark.asyncio
    async def test_critical_alarms_produce_signals(self):
        client = AsyncMock()
        client.get = AsyncMock(side_effect=[
            {"data": HEALTHY_FABRIC},
            {"data": CRITICAL_ALARMS},
        ])

        report = await assess_fabric_health(client)
        alarm_signals = [s for s in report.signals if s.component == "alarms"]
        assert len(alarm_signals) == 1
        assert alarm_signals[0].level == HealthLevel.CRITICAL


class TestCorrelateFabricState:
    @pytest.mark.asyncio
    async def test_full_correlation(self):
        client = AsyncMock()
        client.get = AsyncMock(side_effect=[
            {"data": ONE_EDGE_DOWN},
            {"data": NO_ALARMS},
        ])

        report = await correlate_fabric_state(client)
        assert report.impact is not None
        assert report.impact.scope == "site"  # dc-cedge01 is sole device at site 100
        assert len(report.root_causes) >= 1
        assert len(report.narrative) > 0
        # Narrative should cite data sources
        assert "Data Sources" in report.narrative

    @pytest.mark.asyncio
    async def test_healthy_fabric_has_no_root_causes(self):
        client = AsyncMock()
        client.get = AsyncMock(side_effect=[
            {"data": HEALTHY_FABRIC},
            {"data": NO_ALARMS},
        ])

        report = await correlate_fabric_state(client)
        assert len(report.root_causes) == 0
        assert report.impact.scope == "none"


class TestDiagnoseDevice:
    @pytest.mark.asyncio
    async def test_diagnose_unreachable_device(self):
        async def mock_get(endpoint, params=None):
            if endpoint == "/dataservice/device":
                return {"data": ONE_EDGE_DOWN}
            if "alarms" in endpoint:
                return {"data": NO_ALARMS}
            if "bfd" in endpoint:
                return {"data": []}
            if "control" in endpoint:
                return {"data": []}
            if "status" in endpoint:
                return {"data": []}
            return {"data": []}

        client = AsyncMock()
        client.get = AsyncMock(side_effect=mock_get)

        correlation, device = await diagnose_device(client, "10.10.1.11")
        assert device is not None
        assert device.hostname == "dc-cedge01"
        assert device.overall_health == HealthLevel.CRITICAL

    @pytest.mark.asyncio
    async def test_diagnose_nonexistent_device(self):
        client = AsyncMock()
        client.get = AsyncMock(side_effect=[
            {"data": HEALTHY_FABRIC},  # fabric report: device list
            {"data": NO_ALARMS},       # fabric report: alarm count
            {"data": HEALTHY_FABRIC},  # device health: device list
            {"data": []},              # device health: BFD
            {"data": []},              # device health: control
            {"data": []},              # device health: system status
        ])

        correlation, device = await diagnose_device(client, "99.99.99.99")
        assert device is None


# --- Tests for audit logging ---

class TestAuditRedaction:
    def test_redacts_password(self):
        from cisco_vmanage_mcp.services.audit import _redact
        data = {"username": "admin", "password": "secret123"}
        redacted = _redact(data)
        assert redacted["username"] == "admin"
        assert redacted["password"] == "***REDACTED***"

    def test_redacts_nested(self):
        from cisco_vmanage_mcp.services.audit import _redact
        data = {"config": {"token": "abc123", "host": "example.com"}}
        redacted = _redact(data)
        assert redacted["config"]["token"] == "***REDACTED***"
        assert redacted["config"]["host"] == "example.com"

    def test_truncates_long_strings(self):
        from cisco_vmanage_mcp.services.audit import _redact
        long_str = "x" * 1000
        redacted = _redact(long_str)
        assert len(redacted) < 1000
        assert "truncated" in redacted


# --- Tests for exception taxonomy ---

class TestExceptionTaxonomy:
    def test_error_hierarchy(self):
        from cisco_vmanage_mcp.client import (
            VManageError,
            AuthenticationError,
            VManageAPIError,
            RateLimitError,
            NotFoundError,
            PermissionError,
            ConnectionError,
            TimeoutError,
        )
        # All should be subclasses of VManageError
        assert issubclass(AuthenticationError, VManageError)
        assert issubclass(VManageAPIError, VManageError)
        assert issubclass(RateLimitError, VManageAPIError)
        assert issubclass(NotFoundError, VManageAPIError)
        assert issubclass(PermissionError, VManageAPIError)
        assert issubclass(ConnectionError, VManageError)
        assert issubclass(TimeoutError, VManageError)

    def test_api_error_has_status_code(self):
        from cisco_vmanage_mcp.client import VManageAPIError
        err = VManageAPIError("test", status_code=404, endpoint="/test")
        assert err.status_code == 404
        assert err.endpoint == "/test"


# --- Tests for error handler ---

class TestHandleApiError:
    def test_handles_authentication_error(self):
        from cisco_vmanage_mcp.utils.errors import handle_api_error
        from cisco_vmanage_mcp.client import AuthenticationError
        result = handle_api_error(AuthenticationError("bad creds"))
        assert "Error:" in result
        assert "VMANAGE_USERNAME" in result

    def test_handles_rate_limit(self):
        from cisco_vmanage_mcp.utils.errors import handle_api_error
        from cisco_vmanage_mcp.client import RateLimitError
        result = handle_api_error(RateLimitError("rate limit", 429, "/test"))
        assert "Rate limit" in result

    def test_handles_timeout(self):
        from cisco_vmanage_mcp.utils.errors import handle_api_error
        from cisco_vmanage_mcp.client import TimeoutError
        result = handle_api_error(TimeoutError("timed out"))
        assert "timed out" in result

    def test_handles_unknown_error(self):
        from cisco_vmanage_mcp.utils.errors import handle_api_error
        result = handle_api_error(ValueError("something weird"))
        assert "Unexpected error" in result
