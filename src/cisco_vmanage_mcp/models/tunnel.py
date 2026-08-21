"""Tunnel and BFD Pydantic input models."""

from cisco_vmanage_mcp.models.device import DeviceIdInput

# Tunnel and BFD tools use the same input as device queries (system_ip + response_format)
TunnelInput = DeviceIdInput
BfdInput = DeviceIdInput
OmpPeersInput = DeviceIdInput
