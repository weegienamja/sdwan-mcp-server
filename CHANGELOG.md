# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-03-31

### Added

#### Retrieval Tools (16)
- `vmanage_list_devices` - List all devices in the SD-WAN fabric with status and filters
- `vmanage_get_device_status` - Get detailed status for a specific device by system IP
- `vmanage_get_device_counters` - Get interface error counters and drop stats
- `vmanage_get_device_interfaces` - Get interface list with status, IP, speed, TX/RX
- `vmanage_list_tunnels` - List IPsec tunnels with jitter, latency, loss
- `vmanage_get_bfd_sessions` - Get BFD session status (underlay health checks)
- `vmanage_get_omp_peers` - Get OMP peer list (overlay control plane)
- `vmanage_list_alarms` - List active alarms with severity and time filters
- `vmanage_get_alarm_count` - Get alarm count by severity for quick health checks
- `vmanage_list_events` - List recent system events (config changes, reboots)
- `vmanage_list_policies` - List vSmart policies with activation status
- `vmanage_list_templates` - List device templates with attached device counts
- `vmanage_get_running_config` - Get running configuration for a device (by UUID)
- `vmanage_get_system_status` - Get CPU, memory, disk usage for a device
- `vmanage_get_control_status` - Get control connections (vSmart/vBond connectivity)
- `vmanage_get_fabric_summary` - Get overall fabric health summary (composite tool)

#### Diagnostic Tools (4) - Correlation and Root-Cause Analysis
- `vmanage_assess_fabric_health` - Correlated fabric health assessment with blast radius and root-cause hypotheses
- `vmanage_diagnose_device` - Deep single-device diagnosis with fabric context
- `vmanage_pre_change_validation` - Pre-change go/no-go check with blockers and warnings
- `vmanage_incident_summary` - Audience-aware incident summary (executive or engineer)

#### Version Tool (1)
- `vmanage_check_version` - Check current server version and available updates

#### Correlation Engine
- Per-device health signal computation from raw API data
- Cross-device and cross-site failure correlation
- Blast radius estimation (device vs site vs fabric-wide)
- Ranked root-cause hypothesis generation with confidence levels
- Partial result handling with explicit uncertainty reporting
- Data source citation for every conclusion

#### Infrastructure
- Async HTTP client with session-based auth (JSESSIONID + XSRF token)
- Retry with exponential backoff for transient failures (429, 500-504)
- Compare-and-swap re-authentication for concurrent tool calls
- Structured audit logging with automatic credential redaction
- Exception taxonomy: VManageError hierarchy with actionable messages
- CLI installer for MCP client configuration (Claude Desktop, Claude Code, Cursor)
- CLI companion tool for terminal usage (status, devices, health, alarms, diagnose, smoke-test)
- Anonymous telemetry module (opt-in, Splunk HEC)

#### Testing
- 46 unit tests covering health signals, correlation, audit, and error handling
- All tests use mocked VManageClient (AsyncMock)
