"""Device-related Pydantic input models."""

from pydantic import BaseModel, Field, ConfigDict
from typing import Optional

from cisco_vmanage_mcp.models.common import ResponseFormat


class ListDevicesInput(BaseModel):
    """Input parameters for listing SD-WAN devices."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    device_type: Optional[str] = Field(
        default=None,
        description=(
            "Filter by device type: 'vedge', 'vmanage', 'vsmart', 'vbond'. "
            "NOTE: Both cEdge and vEdge routers have device_type='vedge'. "
            "To distinguish them, use the device_model filter instead."
        ),
        pattern=r"^(vedge|vmanage|vsmart|vbond)$",
    )
    device_model: Optional[str] = Field(
        default=None,
        description=(
            "Filter by device model. Common values: "
            "'vedge-C8000V' (Cisco cEdge/Catalyst 8000V), "
            "'vedge-cloud' (Cisco vEdge Cloud), "
            "'vmanage', 'vsmart'. "
            "Case-insensitive match."
        ),
    )
    reachability: Optional[str] = Field(
        default=None,
        description="Filter by reachability: 'reachable' or 'unreachable'. Leave empty for all.",
        pattern=r"^(reachable|unreachable)$",
    )
    limit: Optional[int] = Field(default=25, ge=1, le=100, description="Max results to return")
    offset: Optional[int] = Field(default=0, ge=0, description="Results to skip for pagination")
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' or 'json'",
    )


class DeviceIdInput(BaseModel):
    """Input for querying a specific device by system IP."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    system_ip: str = Field(
        ...,
        description="System IP address of the device (e.g., '10.10.1.15'). Use vmanage_list_devices to find valid system IPs.",
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' or 'json'",
    )


class DeviceUuidInput(BaseModel):
    """Input for querying a device by UUID."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    device_uuid: str = Field(
        ...,
        description=(
            "Device UUID (not the system IP). "
            "Use vmanage_list_devices first to find the UUID for a device."
        ),
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' or 'json'",
    )
