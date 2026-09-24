# Product Roadmap

This roadmap begins from the audited read-only MCP server and the v1.2 browser Operations Canvas. Dates are planning targets, not external Cisco delivery commitments.

## Product principles

1. Begin with the operator's question, not a product or API selector.
2. Keep conclusions next to evidence, provenance, freshness, and uncertainty.
3. Preserve context when moving between conversation, inventory, topology, and specialist tools.
4. Keep humans in control; recommendations and actions are separate phases.
5. Prefer connector and MCP boundaries over tightly coupling external products into the core.
6. Apply short-lived, least-privilege authorization before introducing write operations.

The research behind these principles is recorded in [2026-09-product-direction.md](research/2026-09-product-direction.md).

## v1.2: Browser Operations Canvas

Status: foundation available in v1.2.0; investigation increment available in v1.2.1

### Foundation

- [x] Local-only Starlette web server and `vmanage-web` entry point.
- [x] Cisco-aligned operational shell with conversation, status band, inventory, assurance path, and evidence rail.
- [x] Deterministic natural-language routing for health, alarms, devices, diagnosis, and change readiness.
- [x] Responsive desktop and mobile layouts.
- [x] Packaged static assets, security headers, and offline API tests.

### Next increments

- [x] Browser session history with bounded, encrypted local persistence.
- [x] Optional Claude, OpenAI, and Gemini bridge using the existing server-side keyring model.
- [x] Streaming response and bounded tool-progress events.
- [x] Investigation pins for devices, alarms, sources, and root-cause hypotheses.
- [x] Exportable sanitized Markdown and JSON incident summaries.
- [x] Keyboard navigation and screen-reader baseline fixes.
- [ ] Independent WCAG AA contrast audit.

Exit criteria:

- Browser module branch coverage at or above 70%.
- No browser exposure outside loopback without authentication and CSRF protections.
- No credentials or provider keys sent to browser storage.
- Playwright checks on desktop and mobile viewports.

## v1.3: Connected Context

Theme: one inventory, one topology, one investigation context.

- [x] Normalized site, controller, edge, TLOC, tunnel, and dependency graph.
- [x] Inventory table with health, reachability, source freshness, and drill-down context.
- [x] Interactive topology focused on fault domains rather than decorative graphing.
- [x] Investigation objects that retain questions, responses, and pinned evidence.
- [x] Actions inbox that groups and prioritizes related alarms and bounded recent events.
- [x] Context handoff links to vManage.
- [x] Configurable credential-free HTTPS handoff links to specialist connectors.

Exit criteria:

- [x] Stable topology schema and golden fixtures.
- [x] Every visual node and edge maps to cited source data.
- [x] Normalization validated at 50 sites and 500 devices.
- [x] Browser rendering benchmark with a representative 50-site and 500-device API fixture.

## v1.4: Assurance and Historical Context

Theme: understand service delivery beyond device status.

- [x] Versioned `FabricSnapshot` model and owner-only encrypted SQLite store.
- [x] Before/after comparison for reachability, alarms, BFD, control, and configuration hashes.
- [x] SLA trends for loss, latency, jitter, and availability from app-route statistics.
- [x] Fault-domain classification across branch, WAN, ISP, Internet, cloud, SaaS, and application.
- [x] Source freshness and partial-evidence indicators.
- [x] Operator-configurable delayed-source thresholds.
- [x] Optional OpenTelemetry export for privacy-bounded operational metrics.

Exit criteria:

- [x] Retention, pruning, migration, and corruption-recovery tests.
- [x] Deterministic comparison reports.
- [x] Synthetic 30-day trend and fault-domain datasets.

## v1.5: Connector and Workflow Platform

Theme: build on tools operators already use.

- [x] Connector registry with common identity, timeout, provenance, classification, and partial-failure contracts.
- [x] ThousandEyes MCP adapter for alerts, tests, and path evidence.
- [ ] Credentialed ThousandEyes environment validation and endpoint-impact field mapping.
- [x] ServiceNow incident drafting from a sanitized investigation bundle.
- [x] Webex and Slack update drafting with explicit local approval.
- [x] Splunk and AppDynamics HTTPS evidence ingestion plus OpenTelemetry snapshot export.
- [x] Deep-link context handoff to configured specialist product workflows.

Exit criteria:

- [x] Per-connector data classification in the registry.
- [ ] Organizational privacy review for each configured remote destination.
- [x] No connector failure prevents core SD-WAN diagnosis.
- [x] Every workflow artifact has preview, approval, expiry, idempotency, and audit records.
- [x] Delivery remains unavailable until destination-specific authorization exists.

## v1.6: Collaborative Investigation Canvas

Theme: humans and specialized agents resolve incidents together.

- [x] Local investigation timeline with comments, ownership, and optimistic revisions.
- [x] Evidence pinning and stale-evidence indicators.
- [x] Editable confidence changes and competing user-authored hypotheses.
- [x] Deterministic Executive, NOC, NetOps, and SecOps projections.
- [x] Purpose-built read-only agent profiles for topology, assurance, security, and change readiness.
- [x] Agent identity registry mapped to a human owner.
- [x] Runtime policy that controls which agent can access each connector and data class.
- [ ] Authenticated remote sharing and multi-user presence.

Exit criteria:

- [ ] Approved authentication provider and non-loopback threat-model review.
- [x] Service-level authorization and tenant-isolation tests.
- [ ] Organizational audit-retention review.
- [x] Conflict and stale-evidence handling.
- [x] Human approval remains mandatory and delivery remains unavailable.

## v2.0: Guarded Change Operations

Theme: plan, approve, apply, verify, and roll back.

- [ ] Separate write-capable deployment mode and credentials.
- [x] `plan` artifacts that are immutable, reviewable, encrypted, and expire.
- [x] Short-lived local human approval bound to actor and exact change hash.
- [ ] Authenticated external authorization bound to actor, intent, targets, and hash.
- [x] Canary scope and blast-radius planning controls.
- [x] Pre-change snapshot binding, post-change validation, and rollback decision support.
- [x] Planning scope limited to device-template attachment and central-policy activation.
- [x] Apply remains structurally disabled; no vManage write operation exists.

Exit criteria:

- [x] Threat model and execution enablement gates documented.
- [ ] Independent security review.
- [x] Synthetic regression verification and rollback recommendation tests.
- [ ] Staging failure injection and rollback execution proof.
- [x] No write-capable endpoint is available in the default read-only mode.
- [x] Major-version planning release; existing read-only MCP and browser APIs remain compatible.

## v2.1: Enterprise Read-Only Deployment

Theme: deploy one hardened Operations Canvas per customer tenant without weakening read-only guarantees.

- [x] Provider-neutral OIDC JWT validation with pinned issuer, audience, tenant, expiry, signing algorithms, and configurable role claims.
- [x] Request-scoped owner, operator, and viewer identities propagated into governance and change-plan authorization.
- [x] Local mode enforced at the request boundary even when an external ASGI server binds broadly.
- [x] HTTPS, public-host, trusted-proxy, cross-site mutation, HSTS, and request-correlation controls.
- [x] Public liveness/readiness endpoints with non-sensitive payloads.
- [x] Non-root multi-stage container and single-replica Kubernetes base with persistent encrypted state.
- [x] Provider-neutral identity-aware proxy deployment contract.
- [ ] Validated customer IdP integration for each supported provider profile.
- [ ] Independent penetration test, WCAG AA audit, load test, and restore exercise.

Exit criteria:

- [x] Local deployments remain loopback-only and API compatible.
- [x] Missing, invalid, wrong-tenant, and unauthorized-role tokens fail closed.
- [x] Direct external access cannot inherit the local owner identity.
- [x] Default Kubernetes workload is non-root, capability-free, and read-only outside its state volume.
- [ ] One production-like customer pilot completes identity, proxy, CA, backup, audit, and SLO acceptance.

## v2.2: Customer Qualification and Multi-Fabric Operations

Theme: make onboarding repeatable across supported Catalyst SD-WAN estates.

- [x] Read-only environment preflight covering authentication, API availability, permissions, software versions, device families, and bounded concurrency.
- [x] Versioned capability report so unavailable endpoints degrade explicitly instead of failing an entire workflow.
- [x] Guided browser setup with test-before-save local credentials, no-restart reconnect, and automatic first-run routing.
- [x] Downloadable sanitized qualification report with full capability detail and aggregate inventory.
- [ ] Customer deployment profile with no embedded secrets.
- [ ] Multiple vManage fabrics per customer deployment with strict fabric selection and evidence provenance.
- [ ] Per-fabric concurrency, rate, timeout, and retention policy.
- [ ] Scheduled snapshots, backup verification, restore tooling, and operational SLO dashboards.
- [ ] Compatibility fixtures for customer-supported Catalyst SD-WAN releases beyond the historical 20.10.1 fixtures and current 20.18.2.1 live qualification.

Exit criteria:

- [x] A new customer environment can be qualified without source changes.
- [ ] Every unsupported capability is visible and does not create false health conclusions.
- [ ] Cross-fabric evidence cannot be mixed without explicit operator selection.
- [ ] Representative scale and degraded-upstream tests meet agreed latency and recovery SLOs.

## v2.3: Cisco Platform Adapter Framework

Theme: extend the evidence model across Cisco estates without pretending one API fits every product.

- [ ] Stable adapter contracts for identity, inventory, topology, assurance, events, configuration evidence, and deep links.
- [ ] Catalyst Center read-only adapter for campus inventory, health, assurance, and topology.
- [ ] Meraki Dashboard read-only adapter for organizations, networks, devices, uplinks, and alerts.
- [ ] Secure Firewall Management Center read-only adapter for health, events, and policy provenance.
- [ ] Cisco ISE read-only adapter for identity and policy context.
- [ ] First-party ThousandEyes qualification against supported alerts, tests, endpoint impact, and path evidence.
- [ ] Cross-domain entity resolution with explicit confidence and source ownership.

Exit criteria:

- [ ] Each adapter has a published version/permission support matrix and sanitized contract fixtures.
- [ ] Product-specific failures remain isolated behind connector boundaries.
- [ ] Cross-domain conclusions cite every source and expose uncertainty.
- [ ] No adapter introduces write methods into the default deployment.

## v2.4: Enterprise Operations and Resilience

Theme: operate the service as a governed customer platform rather than a single host tool.

- [ ] External secret-manager integrations and documented rotation without downtime.
- [ ] External database/object storage backend supporting high availability and tested migration from local encrypted SQLite.
- [ ] Immutable external audit sink with retention, legal-hold, and correlation-ID contracts.
- [ ] Backup, restore, disaster-recovery, and key-recovery runbooks with automated exercises.
- [ ] OpenTelemetry traces and service-level metrics for application latency, dependency health, and saturation.
- [ ] Signed images, SBOM, provenance attestations, vulnerability policy, and release promotion workflow.
- [ ] Customer policy packs for data classification, connector egress, retention, and role mapping.

Exit criteria:

- [ ] Multi-zone failure and dependency-loss exercises meet customer RTO/RPO and SLO targets.
- [ ] Upgrades and schema migrations are reversible and verified against production-scale data.
- [ ] Security, privacy, accessibility, and operational ownership are formally accepted.

## v3.0: Governed Execution Plane

Theme: add narrowly approved network mutations only after enterprise controls are proven.

- [ ] Separate executor service, credentials, network policy, and deployment lifecycle from the read-only plane.
- [ ] External short-lived authorization bound to actor, tenant, intent, operation, target set, and immutable plan hash.
- [ ] Server-side allowlists, idempotency keys, replay prevention, canary enforcement, and bounded concurrency.
- [ ] Staging failure injection for partial apply, timeout, lost connectivity, stale approval, and rollback failure.
- [ ] Automatic post-change evidence collection with explicit stop and rollback decisions.
- [ ] Initial execution remains limited to separately approved Catalyst SD-WAN workflows.

Exit criteria:

- [ ] All gates in the threat model are independently reviewed and evidenced.
- [ ] Customer change authority approves the operation-specific runbook and rollback proof.
- [ ] The read-only deployment remains independently installable and contains no write credentials.

## External readiness gates

These remain prerequisites rather than coding tasks:

- Approved CA chain and verified production TLS.
- Production-like load and soak window with agreed SLOs.
- Cisco SD-WAN owner sign-off on health and correlation semantics.
- Privacy approval before Cisco IDE telemetry is enabled.
- Authenticated Cisco marketplace manifest validation.
- Product-specific legal, privacy, API-support, and data-residency review before enabling each Cisco adapter.
