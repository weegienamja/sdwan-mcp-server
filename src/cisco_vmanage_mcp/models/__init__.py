"""Optional Pydantic helpers mirroring common flat MCP tool arguments."""

from cisco_vmanage_mcp.models.alarm import (
	AlarmCountInput,
	ListAlarmsInput,
	ListEventsInput,
)
from cisco_vmanage_mcp.models.common import PaginationInput, ResponseFormat
from cisco_vmanage_mcp.models.device import (
	DeviceIdInput,
	DeviceUuidInput,
	ListDevicesInput,
)
from cisco_vmanage_mcp.models.policy import ListPoliciesInput, ListTemplatesInput
from cisco_vmanage_mcp.models.tunnel import BfdInput, OmpPeersInput, TunnelInput

__all__ = [
	"AlarmCountInput",
	"BfdInput",
	"DeviceIdInput",
	"DeviceUuidInput",
	"ListAlarmsInput",
	"ListDevicesInput",
	"ListEventsInput",
	"ListPoliciesInput",
	"ListTemplatesInput",
	"OmpPeersInput",
	"PaginationInput",
	"ResponseFormat",
	"TunnelInput",
]
