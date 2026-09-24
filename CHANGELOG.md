# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.2.0] - 2026-09-08

### Added

- Four-stage browser Setup workspace covering deployment mode, vManage connection, environment qualification, optional integrations, and MCP client registration
- Local-owner test-before-save workflow with atomic owner-only credential persistence and immediate no-restart reconnection
- Full in-product qualification results with readiness, aggregate inventory, software versions, device families, all capability endpoints, row counts, latency, and failure reasons
- Downloadable sanitized JSON qualification report and exact non-secret configuration-key guidance for optional integrations
- Automatic first-run routing to Setup when vManage credentials or startup validation are unavailable

### Security

- Never return setup usernames or passwords from status, test, apply, or qualification endpoints
- Keep enterprise connection changes deployment-managed and reject browser attempts to replace enterprise secrets
- Preserve explicit process environment precedence while allowing the private setup file to override development dotenv values on restart

### Changed

- Hide operational status metrics while Setup is active and provide clear connected, pending, and attention states
- Add a copyable `vmanage-mcp-install` handoff for supported MCP clients

### Testing

- Expand the suite to 368 tests covering secure persistence, API validation, reconnect behavior, enterprise denial, disconnected first-run routing, and qualification failure paths
- Validate connected and credential-free setup flows at desktop and mobile sizes with no page overflow or browser errors

## [2.1.1] - 2026-09-08

### Changed

- Replace the approximate five-bar header mark and typed brand name with the approved modern Cisco vector logo and exact outlined wordmark
- Self-host CiscoSansTT Thin, Light, Regular, Medium, and Bold for deterministic Cisco typography across customer deployments
- Remove Google Fonts requests and restrict browser style and font loading to same-origin assets

### Testing

- Verify logo geometry, local font delivery, CSP restrictions, desktop and mobile layout, and loaded CiscoSans browser faces

## [2.1.0] - 2026-09-08

### Added

- Provider-neutral OIDC bearer validation with pinned issuer, audience, tenant, lifetime, asymmetric algorithms, JWKS metadata, configurable roles, and optional access-token discrimination
- Request-scoped owner, operator, and viewer authorization for the browser Operations Canvas
- Trusted-host, proxy-network, HTTPS, cross-site mutation, request-size, request-ID, and browser audit controls
- Tenant-bound encrypted state, public liveness/readiness endpoints, and bounded identity-provider retry behavior
- Non-root multi-stage container and hardened single-replica Kubernetes deployment base
- `vmanage-mcp qualify` for sanitized customer environment, version, device-family, permission, latency, and API-capability assessment
- Enterprise deployment guide and roadmap through multi-fabric operations, Cisco platform adapters, resilient infrastructure, and a separately governed execution plane

### Security

- Enforce local-mode loopback at request time even when an external ASGI server is misbound
- Reject missing, expired, wrong-audience, wrong-tenant, wrong-role, disallowed-algorithm, and unknown-key tokens
- Accept forwarded HTTPS only from explicitly configured proxy IPs or CIDRs
- Bind workflow lifecycle actors to verified OIDC identities and keep live network execution disabled

### Testing

- Expand the suite to 357 tests covering identity, proxy trust, tenant state, role policy, deployment artifacts, audit privacy, request bounds, and environment qualification
- Qualify the current Cisco sandbox with seven devices and all twelve read-only capability probes available

### Known Limits

- Enterprise mode supports one customer tenant and one application replica per deployment while encrypted SQLite remains the persistence backend
- An approved identity-aware HTTPS proxy is required; customer IdP, CA, role, backup, load, accessibility, privacy, and penetration-test acceptance remain deployment gates
- Current managed-platform support is Cisco Catalyst SD-WAN through vManage; other Cisco product families remain adapter roadmap work
- The package contains no vManage write executor and Apply remains disabled

## [2.0.2] - 2026-09-08

### Changed

- Consolidate owner-only directory, Fernet key, SQLite connection, canonical JSON, and hash handling across all encrypted browser stores
- Decompose AI login, console startup and command handling, HTTP retry policy, topology assembly, device-health enrichment, and telemetry metric projection into focused units
- Share lazy browser-store initialization and client-side collection item replacement without changing API contracts

### Removed

- Remove duplicated persistence implementations, redundant frontend transition code, and an unused server logger

### Testing

- Add direct persistence primitive and device-detail health-signal regression coverage
- Revalidate all 313 tests, coverage thresholds, static analysis, security checks, package builds, and responsive browser behavior

## [2.0.1] - 2026-09-07

### Added

- Competing user-authored hypotheses with revisioned low, medium, and high confidence changes
- Operator-configurable source-delay classification through `VMANAGE_SOURCE_STALE_SECONDS`
- Optional credential-free HTTPS handoff links for configured ThousandEyes, Splunk, and AppDynamics workflows

### Changed

- Complete the browser AI roadmap item with metadata-gated Evidence and AI explain modes
- Validate the topology renderer with a synthetic 50-site/500-device graph, rendered as 50 aggregate site nodes without horizontal overflow

### Testing

- Add collaboration hypothesis, confidence, source freshness, connector handoff, and large-fabric rendering checks

## [2.0.0] - 2026-09-07

### Added

- Encrypted immutable change plans for device-template attachment and central-policy activation intent
- Explicit targets, canary subsets, pre-change snapshot binding, expected outcomes, rollback strategy, expiry, and canonical plan hashes
- Short-lived human approval bound to the exact plan hash
- Post-change snapshot verification with deterministic improved, unchanged, mixed, or regressed outcomes
- Rollback recommendation when verification detects mixed or regressed fabric evidence
- Planning-only Changes workspace with create, approve, verify, cancel, and disabled Apply controls
- Guarded-change threat model and explicit execution enablement gates

### Security

- Keep live execution structurally disabled: the plan store has no apply method and the Apply API always returns HTTP 403
- Reject agent approvals and cross-tenant plan access
- Preserve plan hashes across approval and verification lifecycle updates

### Known Limits

- This release does not perform vManage writes, canary deployment, or rollback execution
- Enterprise authentication, short-lived external authorization, independent security review, and staging failure injection remain mandatory before a write-capable deployment can exist

## [1.6.0] - 2026-09-07

### Added

- Encrypted collaboration records with local ownership, comments, bounded timelines, and optimistic revision checks
- Tenant-aware role policy for owners, operators, viewers, and human-owned agent identities
- Read-time stale-evidence flags with explicit observation time and freshness threshold
- Deterministic Executive, NOC, NetOps, and SecOps investigation projections with audience-specific data reduction
- Four built-in read-only agent profiles for topology, assurance, security, and change readiness
- Runtime agent policy for connector allowlists and maximum data classification
- Browser collaboration timeline, owner control, audience selector, and agent registry

### Security

- Keep the browser loopback-only and identify the current session as `local-owner`; client-supplied identity headers are not trusted
- Enforce tenant isolation and role permissions in the governance service
- Reject stale concurrent timeline/ownership updates with revision conflicts

### Known Limits

- Enterprise authentication, remote multi-user sharing, and non-loopback serving remain unavailable pending an approved identity provider, CSRF controls, TLS termination, and tenant-isolation review

## [1.5.0] - 2026-09-07

### Added

- Connector registry with identity, capability, timeout, provenance, data classification, and partial-failure contracts
- HTTPS Streamable MCP transport and ThousandEyes alert, test, and path-evidence normalization
- Strict HTTPS JSON evidence adapters for opt-in Splunk and AppDynamics feeds
- Encrypted ServiceNow, Webex, and Slack workflow previews with expiry, idempotency, exact-hash approval, cancellation, and audit records
- Integrations workspace for connector state, explicit evidence collection and pinning, and local workflow draft review
- Optional stateless Claude, OpenAI, or Gemini browser explanations over bounded normalized evidence

### Security

- Keep all connector and provider credentials server-side and exclude them from descriptors, browser storage, drafts, and logs
- Isolate connector timeout and failure from core vManage diagnosis
- Reject insecure or credential-bearing remote connector URLs without preventing local application startup
- Preserve deterministic evidence alongside optional generated explanations
- Keep workflow delivery unavailable; approved drafts remain local preview artifacts and `/send` is intentionally absent

### Testing

- Cover connector failure/timeout isolation, disabled configuration, provenance rewriting, HTTP authentication privacy, workflow immutability, approval hashes, expiry, and no-send behavior
- Validate the default live posture with vManage available and ThousandEyes, Splunk, AppDynamics, and browser AI visibly unconfigured

## [1.4.0] - 2026-09-07

### Added

- Versioned immutable `FabricSnapshot` records in an owner-only encrypted SQLite store
- Count- and age-based retention, deterministic schema migration, and unreadable-record quarantine
- Before/after comparison for device reachability, health, alarms, BFD, control connections, and configuration hashes
- Bounded app-route SLA collection for latency, jitter, loss, and derived availability
- Thirty-day branch, WAN, ISP, Internet, cloud, SaaS, and application fault-domain trends
- Optional privacy-bounded OpenTelemetry metrics through the official OTLP HTTP exporter
- Browser assurance workflow for capture, history, comparison, deletion, and trend inspection

### Security

- Running configurations are hashed in memory only when explicitly requested; raw configuration text is never stored or returned by snapshot APIs
- OpenTelemetry is disabled by default and excludes hostnames, system IPs, raw alarms, and configurations

### Fixed

- Emit browser evidence timestamps as UTC ISO 8601 values instead of epoch seconds interpreted as browser milliseconds
- Use the verified vManage app-route statistics endpoint for live SLA measurements

## [1.3.0] - 2026-09-07

### Added

- Versioned, source-cited topology schema for sites, controllers, edges, TLOCs, BFD tunnels, and control relationships
- Bounded asynchronous topology collection with partial-source handling and a golden schema fixture
- Interactive controller, transport, site, and relationship topology views with evidence inspection
- Prioritized actions inbox that correlates alarms and recent events by site and fault family
- Context-preserving site inventory and device diagnosis handoffs, plus a validated HTTPS vManage origin link

### Changed

- Bound actions to a server-side and locally enforced 24-hour event window with a 1,000-row ceiling
- Deduplicate representative action evidence while retaining complete occurrence counts and severity ordering
- Expand browser navigation for dedicated Topology and Actions workspaces

### Testing

- Validate deterministic topology IDs and relationships against a golden fixture
- Validate normalization at 50 sites and 500 devices, bounded collection concurrency, and partial source failures
- Validate live DevNet topology/actions behavior at desktop and mobile viewports with no horizontal overflow or browser errors

## [1.2.1] - 2026-09-07

### Added

- Versioned `/api/v1` browser routes with compatibility aliases for the original metadata, overview, and chat endpoints
- Owner-only, encrypted SQLite investigation history with schema versioning, bounded retention, and deterministic loading
- Streamed assistant progress events with cancellation propagation and persisted normalized responses
- Evidence pinning for devices, alarms, sources, and root-cause hypotheses
- Sanitized Markdown and JSON investigation exports

### Changed

- Restore investigation conversations across browser reloads and expose a session selector
- Add explicit evidence freshness, request cancellation, retry controls, and restrained conversation auto-scroll
- Improve keyboard and screen-reader behavior with skip navigation, busy state, assertive errors, status labels, and an inventory table caption

### Security

- Keep investigation files and encryption keys owner-only and redact credential-shaped fields before persistence and export
- Persist normalized evidence only; raw vManage responses and provider credentials are excluded

## [1.2.0] - 2026-09-07

### Added

- Local-only `vmanage-web` browser Operations Canvas built on Starlette and Uvicorn
- Conversational evidence routing for fabric health, devices, alarms, device diagnosis, and change readiness
- Live posture band, investigation timeline, evidence rail, inventory view, and assurance roadmap view
- Responsive Cisco-aligned visual system with operational status semantics and CiscoSans-first typography
- Browser security headers, no-store API responses, and loopback-only binding
- Research brief covering Cisco AgenticOps, AI Canvas, Cloud Control, AI Assistant, ThousandEyes assurance, MCP workflows, and agent security
- Phased product roadmap from the browser canvas through connected context, assurance, workflow integrations, collaboration, and guarded writes
- Browser API, asset, offline-state, and local-bind tests with an 80% module coverage gate

### Changed

- Added direct Uvicorn runtime dependency and packaged browser assets
- Extended the clean package with a fifth console entry point, `vmanage-web`

## [1.1.0] - 2026-09-07

### Security

- Enable TLS verification by default, reject invalid TLS/retry settings, support private CA bundles, and remove the version check's TLS bypass
- Store installer-managed vManage credentials in an owner-only dotenv file instead of MCP JSON or process arguments
- Store optional AI API keys in the OS keyring, hide key entry, and require explicit use of packaged enterprise interception roots
- Remove the unused arbitrary POST client method and runtime shell/package-install execution
- Redact secret-key variants, log result metadata instead of content, and write private rotating audit files
- Replace username-derived telemetry hashes with random installation identifiers and make all telemetry paths explicit opt-ins
- Raise runtime dependencies to vulnerability-free security floors

### Fixed

- Correct CLI, interactive console, and AI-provider handling of the device-diagnosis service tuple
- Fail pre-change validation closed when required evidence is incomplete or the device inventory is empty
- Treat empty or partial health evidence as unknown unless a known degraded or critical signal exists
- Honor bounded `Retry-After` values and normalize malformed API JSON into the vManage exception taxonomy
- Replace the incompatible Cisco IDE FastMCP middleware with the SDK's supported standalone client lifecycle
- Correct and commit-pin the internal marketplace SDK distribution, and package the optional Cisco CA bundle

### Changed

- Document and register 21 tools: 16 retrieval, 4 diagnostic, and 1 version tool
- Audit every registered tool and emit opt-in telemetry through the shared wrapper
- Bound each AI provider conversation to ten consecutive tool-call rounds
- Add typed MCP annotations, `py.typed`, secure installer output, and explicit core/AI/marketplace dependency extras

### Testing

- Add client, tool, diagnostic, entry-point, installer, audit, telemetry, AI credential, and version-boundary tests
- Add Ruff, Pyright, Bandit, runtime dependency audit, 60% core branch coverage, build/Twine, pre-commit, and Python 3.11-3.14 CI gates
- Add command-level coverage for every CLI command and platform/error/orchestration coverage for the installer, with separate 60% module gates
- Add offline console lifecycle and Claude/OpenAI/Gemini tool-cycle suites, with separate 60% coverage gates for both optional adapters
- Preserve configured audit-file logging when the interactive console suppresses stderr noise
- Export and document optional Pydantic validation helpers with a 95% package coverage gate
- Bound event retrieval with verified GET query rules and normalize live underscore field names

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
