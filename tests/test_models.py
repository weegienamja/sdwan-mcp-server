"""Contracts for the optional public Pydantic input helpers."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cisco_vmanage_mcp.models import (
    AlarmCountInput,
    BfdInput,
    DeviceIdInput,
    DeviceUuidInput,
    ListAlarmsInput,
    ListDevicesInput,
    ListEventsInput,
    ListPoliciesInput,
    ListTemplatesInput,
    OmpPeersInput,
    PaginationInput,
    ResponseFormat,
    TunnelInput,
)


def test_common_defaults_and_enum_conversion() -> None:
    pagination = PaginationInput.model_validate({"response_format": "json"})
    alarm_count = AlarmCountInput()

    assert pagination.limit == 25
    assert pagination.offset == 0
    assert pagination.response_format is ResponseFormat.JSON
    assert alarm_count.response_format is ResponseFormat.MARKDOWN


@pytest.mark.parametrize(
    ("model", "values"),
    [
        (PaginationInput, {"limit": 0}),
        (PaginationInput, {"limit": 101}),
        (PaginationInput, {"offset": -1}),
        (ListDevicesInput, {"device_type": "router"}),
        (ListDevicesInput, {"reachability": "unknown"}),
        (ListAlarmsInput, {"severity": "Emergency"}),
        (ListAlarmsInput, {"hours_back": 169}),
        (ListEventsInput, {"hours_back": 0}),
        (ListPoliciesInput, {"extra_field": True}),
    ],
)
def test_invalid_values_are_rejected(model, values) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(values)


def test_device_models_strip_strings_and_forbid_extras() -> None:
    listing = ListDevicesInput(
        device_type="vedge",
        device_model="  vedge-C8000V  ",
        reachability="reachable",
    )
    device = DeviceIdInput.model_validate(
        {"system_ip": " 10.0.0.1 ", "response_format": "json"}
    )
    device_uuid = DeviceUuidInput(device_uuid=" device-uuid ")

    assert listing.device_model == "vedge-C8000V"
    assert device.system_ip == "10.0.0.1"
    assert device.response_format is ResponseFormat.JSON
    assert device_uuid.device_uuid == "device-uuid"
    with pytest.raises(ValidationError):
        DeviceIdInput.model_validate({"system_ip": "10.0.0.1", "unexpected": True})


def test_alarm_policy_and_template_models_serialize() -> None:
    alarms = ListAlarmsInput(severity="Critical", hours_back=1, limit=100)
    events = ListEventsInput(system_ip="10.0.0.1")
    policies = ListPoliciesInput(offset=10)
    templates = ListTemplatesInput(device_type="vedge")

    assert alarms.model_dump(mode="json")["severity"] == "Critical"
    assert events.system_ip == "10.0.0.1"
    assert policies.offset == 10
    assert templates.device_type == "vedge"


def test_tunnel_aliases_share_device_contract() -> None:
    assert TunnelInput is DeviceIdInput
    assert BfdInput is DeviceIdInput
    assert OmpPeersInput is DeviceIdInput
    assert TunnelInput(system_ip="10.0.0.1").system_ip == "10.0.0.1"


def test_models_publish_closed_json_schemas() -> None:
    for model in (
        PaginationInput,
        ListDevicesInput,
        DeviceIdInput,
        DeviceUuidInput,
        ListAlarmsInput,
        AlarmCountInput,
        ListEventsInput,
        ListPoliciesInput,
        ListTemplatesInput,
    ):
        assert model.model_json_schema()["additionalProperties"] is False
