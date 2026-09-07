# Cisco vManage MCP Server Roadmap

This roadmap describes a possible evolution of the project from a read-only Cisco SD-WAN MCP integration into an evidence-driven network investigation and operations platform.

The direction is deliberately broader than adding more vManage API wrappers. The goal is to preserve the project's existing design principle:

> **APIs gather facts. Python computes signals. The LLM explains and investigates the evidence.**

The long-term target is an **SD-WAN Investigator** that can autonomously gather evidence, test competing hypotheses, reconstruct incidents, reason about topology and changes, and eventually propose tightly controlled remediation.

This is a directional roadmap rather than a commitment to specific release dates.

---

## Current baseline

The project already provides the foundations for this direction:

- read-only Cisco SD-WAN vManage integration
- structured MCP tool surface
- device, tunnel, BFD, OMP, alarm, policy and configuration retrieval
- deterministic fabric health computation
- cross-device and cross-site correlation
- failure-scope classification
- blast-radius estimation
- ranked root-cause hypotheses
- explicit evidence provenance
- partial-result and uncertainty handling
- pre-change validation
- incident summaries
- audit logging and credential redaction
- multi-provider AI console support

The next stages focus on making the system capable of conducting investigations rather than only answering individual queries.

---

# Guiding principles

## 1. Deterministic facts, model-driven investigation

Network state, thresholds, comparisons, topology calculations, evidence relationships and safety controls should remain normal application logic wherever practical.

The model should primarily be responsible for:

- deciding what evidence to request next
- managing competing hypotheses
- adapting an investigation to new observations
- identifying gaps and uncertainty
- explaining findings to different audiences
- proposing safe next actions

## 2. Evidence before conclusions

Every important conclusion should be traceable to observations gathered from known sources.

The system should distinguish:

- observed facts
- calculated signals
- hypotheses
- model interpretation
- recommended actions

## 3. Read-only until a separate write safety model exists

The current read-only boundary should remain intact through the investigation-focused releases.

Future write capability should not be implemented by simply exposing vManage POST, PUT or DELETE endpoints to an LLM. Any remediation layer should require separate controls for simulation, validation, approval, execution, verification and rollback.

## 4. Provider independence

The runtime should support interchangeable AI providers and configurable model selection rather than relying on hard-coded model names.

More capable reasoning models can be used for deeper investigations, while deterministic services remain portable across providers.

---

# Proposed releases

## v1.1 - Unified AI tool surface

**Goal:** Give the integrated AI console access to the same useful read-only capabilities exposed through MCP.

### Planned work

- replace the smaller console-specific tool list with a shared tool registry
- expose device, tunnel, BFD, OMP, interface, control, policy, template and configuration tools to the integrated AI provider layer
- share schemas between MCP and provider integrations where practical
- standardise structured tool results
- preserve read-only annotations and safety semantics
- add provider-level tool execution tests

### Outcome

The embedded AI experience should no longer be materially less capable than an external MCP client.

---

## v1.2 - Pluggable model configuration

**Goal:** Remove hard-coded model assumptions and make reasoning capability configurable.

### Planned work

- configurable model name per provider
- environment and config-file model selection
- optional reasoning-effort or provider-specific inference settings where supported
- model capability metadata
- sensible fallbacks for providers without advanced reasoning controls
- token and tool-call accounting
- optional per-investigation model selection

### Example

```text
Provider: OpenAI
Model: configurable
Reasoning profile: high
Tool budget: 20
Investigation budget: 60 seconds / configurable
```

The project should not assume that access to a model through a consumer product also provides API entitlement.

---

## v1.3 - Autonomous Investigator

**Goal:** Move from one-shot troubleshooting to iterative, evidence-driven investigation.

This is the most important architectural milestone.

### Investigation loop

```text
Symptom
  -> initial hypotheses
  -> select diagnostic action
  -> gather evidence
  -> update hypotheses
  -> identify best next test
  -> repeat until confidence / budget threshold
  -> final assessment
```

### Planned work

- `Investigation` domain model
- explicit `Evidence` records
- explicit `Hypothesis` records
- supporting and contradicting evidence
- confidence updates
- tool-call budget and investigation depth limits
- investigation stop conditions
- unresolved-question tracking
- next-best-diagnostic-action selection
- investigation audit trail

### Example

```text
Incident symptom:
Intermittent Microsoft Teams degradation at Glasgow.

Hypotheses:
1. DIA transport degradation       35%
2. BFD instability                25%
3. Edge resource saturation       15%
4. Application-route policy       15%
5. Controller issue               10%

Evidence gathered:
- DIA packet loss: 7.8%
- BFD flaps: 13 in 42 minutes
- MPLS healthy
- control plane healthy
- edge CPU normal

Final hypothesis:
DIA transport degradation
Confidence: HIGH
```

The investigator should not stop at the first plausible explanation when inexpensive evidence could distinguish it from alternatives.

---

## v1.4 - Network Flight Recorder

**Goal:** Add historical state so investigations can reason about what changed before an incident.

### Planned work

Periodic snapshots of selected state including:

- device reachability
- BFD sessions
- OMP peers
- control connections
- tunnel health and SLA metrics
- interface counters
- alarms and events
- system resource state
- policy/template state
- configuration hashes

### Storage

Start with a local persistence layer suitable for development and small environments, with an abstraction that permits external storage later.

Potential options:

- SQLite
- PostgreSQL
- time-series backends through adapters

### Outcome

The system should be able to answer:

> What changed immediately before the outage?

Example reconstruction:

```text
13:42:11  Policy template modified
13:44:03  Template pushed to GLA-EDGE-01
13:44:27  DIA BFD session down
13:44:31  Application route changed
13:44:39  14 tunnels affected
13:45:02  Critical alarm generated
```

---

## v1.5 - Change Intelligence

**Goal:** Turn pre-change validation into full before/after change analysis.

### Planned work

- named pre-change snapshots
- post-change snapshot capture
- deterministic state diff
- expected vs unexpected change classification
- semantic configuration summarisation
- regression detection
- rollback recommendation signals
- change evidence bundle

### Example

```text
CHANGE IMPACT ANALYSIS

Expected
- template revision 14 -> 15
- policy attachment changed on GLA-EDGE-01

Unexpected
- BFD session count 14 -> 12
- DIA loss increased 0.1% -> 3.8%
- Microsoft Teams application route changed
- two new warning alarms

Recommendation: ROLLBACK
Confidence: HIGH
```

### Semantic configuration diff

Instead of presenting only raw line diffs, produce an operator-level interpretation of configuration changes while retaining the underlying deterministic diff as evidence.

---

## v1.6 - Topology and Evidence Graph

**Goal:** Give the system an explicit model of network relationships rather than disconnected API payloads.

### Planned work

Represent relationships between objects such as:

- sites
- devices
- transports
- interfaces
- tunnels
- control sessions
- service VPNs
- policies
- applications
- destinations
- evidence observations

Potential implementation: NetworkX initially, with a graph abstraction that can later support other stores.

### Capabilities

- overlay path tracing
- dependency analysis
- topology-aware blast radius
- single points of failure
- transport dependency analysis
- controller dependency analysis
- path-aware incident reasoning

### Example questions

- Which path should Teams traffic take from Glasgow to London?
- What happens to this site if MPLS is lost?
- Which sites depend exclusively on this transport?
- Which devices are inside the blast radius of a controller failure?

---

## v1.7 - Baselines and anomaly detection

**Goal:** Compare network behaviour to its own historical baseline instead of relying only on static thresholds.

### Planned work

Baselines by combinations such as:

- tunnel
- site
- transport
- application
- hour of day
- day of week

Signals can include:

- latency
- jitter
- packet loss
- BFD flap rate
- interface errors
- utilisation
- CPU and memory

The anomaly calculation remains deterministic. The model interprets why an anomaly may matter in the current network context.

### Example

```text
GLA -> LON DIA

Current latency: 71 ms
Static classification: healthy
Historical baseline: 18-27 ms
Deviation: +188%
Anomaly classification: significant
```

---

## v1.8 - Persistent Incident Workspace

**Goal:** Make investigations durable operational objects rather than transient chat conversations.

### Planned work

Each incident can retain:

- symptom and impact
- affected sites/devices
- investigation timeline
- evidence
- hypotheses
- confidence history
- operator notes
- unresolved questions
- recommended actions
- final diagnosis
- resolution

### Audience-specific outputs

Generate structured views for:

- NOC engineer
- network engineering handoff
- incident manager
- executive update
- TAC escalation
- post-incident review

### Historical incident memory

Once sufficient incident history exists, add similarity search so the investigator can identify previous incidents with comparable symptoms and evidence patterns.

---

## v1.9 - Documentation and Runbook Intelligence

**Goal:** Combine live network evidence with controlled operational knowledge.

### Version-aware documentation retrieval

Documentation retrieval should be filtered by the actual SD-WAN/vManage version where possible so that guidance for newer software is not silently applied to older deployments.

Potential sources may include:

- Cisco product documentation
- API documentation
- validated internal runbooks
- operator-provided documentation

### Runbook compiler

Allow an engineer to describe a troubleshooting process and compile it into a constrained diagnostic workflow.

Example:

```yaml
name: edge_connectivity_failure

steps:
  - get_device_status
  - if: device_unreachable
    run:
      - get_control_connections
      - get_bfd_sessions
      - get_interfaces
```

Generated runbooks should be validated before use against:

- allowed tools
- argument schemas
- read-only restrictions
- execution depth
- execution budget
- expected output types

---

## v2.0 - Guarded Remediation

**Goal:** Introduce tightly controlled write capability without giving the model unrestricted configuration access.

This requires a separate safety architecture.

### Proposed execution lifecycle

```text
OBSERVE
  -> DIAGNOSE
  -> PROPOSE
  -> SIMULATE
  -> VALIDATE
  -> HUMAN APPROVAL
  -> EXECUTE
  -> VERIFY
  -> ROLLBACK if required
```

### Required controls

- explicit allow-listed change operations
- mandatory pre-change snapshot
- blast-radius calculation
- policy and scope validation
- human approval token / workflow
- immutable audit record
- post-change verification
- defined rollback operation
- automatic halt when observed state diverges from expected state
- no unrestricted arbitrary API execution by the model

### Example

```text
PROPOSED REMEDIATION

Action:
Change preferred transport for Teams from DIA to MPLS.

Scope:
Site 101 only

Expected impact:
Approximately 320 users

Pre-check:
PASS

Rollback:
Policy version 17

Risk:
MEDIUM

Execution:
REQUIRES OPERATOR APPROVAL
```

---

# Parallel workstreams

The following initiatives can progress alongside the release sequence.

## Root-cause competition

Develop the current ranked hypothesis system into an explicit diagnostic competition model.

The investigator should ask not only:

> What is probably wrong?

but also:

> What evidence would most efficiently distinguish the leading hypotheses?

Example:

```text
H1 ISP outage       51%
H2 Edge failure     34%
H3 vSmart issue     15%

Best discriminating test:
Check BFD state on the independent transport.
```

A formal Bayesian implementation is optional. The important requirement is explicit evidence-driven hypothesis revision rather than unsupported confidence scoring.

## Counterfactual analysis

Use topology and dependency data to reason about proposed failures or maintenance before anything is changed.

Example questions:

- What happens if vSmart-02 is taken offline?
- What is the blast radius if this transport fails?
- Can this site tolerate loss of its primary circuit?
- Which redundancy assumptions are currently false?

## Cross-domain evidence providers

Over time, decouple investigation evidence from vManage-specific assumptions.

Potential adapters:

- Cisco Catalyst SD-WAN Manager
- Cisco Catalyst Center
- Cisco Meraki
- Cisco ThousandEyes
- Splunk
- ServiceNow
- Prometheus
- syslog
- SNMP
- NetFlow/IPFIX

Longer term, the investigator should be able to correlate evidence from multiple operational systems while preserving source provenance.

## Operator UI

The long-term interface should treat chat as one part of an investigation workspace rather than the entire product.

Potential panels:

- topology
- active incidents
- hypothesis ranking
- evidence timeline
- blast radius
- change history
- network health
- investigation chat

## Network engineering benchmark

Create a repeatable benchmark suite with synthetic and lab-generated failure scenarios.

Potential scenarios:

1. single-site transport outage
2. controller outage
3. intermittent BFD instability
4. bad policy deployment
5. resource exhaustion
6. multi-site ISP degradation
7. misleading concurrent alarms
8. partial telemetry loss
9. configuration drift
10. asymmetric failure affecting only one application path

Metrics can include:

- correct fault-domain classification
- affected-site accuracy
- unsupported-claim rate
- correct next diagnostic action
- tool-call efficiency
- time to sufficient confidence
- unnecessary tool calls
- behaviour with missing evidence

This can be used to compare model providers, model versions and deterministic-only baselines.

---

# Suggested architecture evolution

```text
AI Clients / Operator UI
          |
          v
Investigation Orchestrator
          |
          +---------------------+
          |                     |
          v                     v
Hypothesis Engine          Runbook Engine
          |                     |
          +----------+----------+
                     |
                     v
              Evidence Layer
                     |
       +-------------+-------------+
       |             |             |
       v             v             v
  Live vManage   Historical    Documentation
     APIs         Snapshots       / Runbooks
       |             |
       +------+------+ 
              |
              v
     Deterministic Services
  health / topology / diff /
  baselines / blast radius
```

The model should remain outside the trusted computation boundary for facts and safety-critical state transitions.

---

# Near-term implementation priority

If development capacity is limited, prioritise these three capabilities:

1. **Autonomous Investigator**
2. **Network Flight Recorder**
3. **Topology and Evidence Graph**

Together they enable substantially deeper workflows than a conventional chat interface:

> Investigate why Glasgow application performance degraded, determine when it began, reconstruct what changed beforehand, test competing fault hypotheses, calculate the blast radius and recommend the safest next action without stopping at the first plausible explanation.

---

# Definition of success

The project should increasingly be evaluated by operational outcomes rather than number of API tools.

Useful measures include:

- percentage of incidents where the correct fault domain is identified
- percentage of conclusions with explicit evidence provenance
- unsupported-claim rate
- average diagnostic tool calls to reach sufficient confidence
- ability to identify missing evidence instead of guessing
- blast-radius accuracy
- pre/post-change regression detection accuracy
- operator time saved during incident triage
- rollback recommendation accuracy in lab scenarios

The intended end-state is not an autonomous network controller. It is an **evidence-driven network investigator** that can progressively automate the expensive reasoning work around incident diagnosis and change assurance while keeping network state, provenance and operational safety explicit.