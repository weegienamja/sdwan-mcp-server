# Product Direction Research

Research date: 2026-09-07

This brief records external product signals used to shape the browser workspace and roadmap. Announced or preview capabilities are not represented as shipped features in this project.

## Cisco findings

### Unified operations and AgenticOps

Cisco's June 10, 2025 secure-network announcement describes unified management across cloud, on-premises, and hybrid deployments, with ThousandEyes assurance, a domain-specific Deep Network Model, Cisco AI Assistant, and AI Canvas. The announcement described AI Assistant as public beta and AI Canvas as entering select-customer testing in fall 2025.

Product implications:

- Start investigations from operator intent, not from an API or dashboard selection.
- Keep human operators in control of recommendations and future actions.
- Treat cross-domain evidence and dependency mapping as first-class objects.
- Avoid presenting announced autonomous behavior as available before it is verified.

Source: [Cisco Unveils Secure Network Architecture to Accelerate Workplace AI Transformation](https://newsroom.cisco.com/c/r/newsroom/en/us/a/y2025/m06/cisco-unveils-secure-network-architecture-to-accelerate-workplace-ai-transformation.html), June 10, 2025.

### AI Assistant and collaborative Canvas

Cisco describes AI Assistant as an assistive conversational interface for issue identification, root-cause diagnosis, configuration assistance, and supervised agentic workflows. AI Canvas is described as a shared generative workspace where people and agents investigate and act across domains with context intact.

Product implications:

- Separate conversational summaries from cited evidence.
- Model an investigation as a durable collection of evidence, hypotheses, and next steps.
- Require explicit approval for any future write action.
- Add shareable investigations and collaboration only after identity and authorization exist.

Sources:

- [From AgenticOps to Assurance: Redefining Network Operations](https://blogs.cisco.com/networking/from-agenticops-to-assurance-redefining-network-operations), June 10, 2025.
- [Welcome to the Agentic Era: Humans + Agents Achieving More, Together](https://blogs.cisco.com/news/welcome-to-the-agentic-era-humans-agents-achieving-more-together), June 10, 2025.
- [Cisco AI Assistant](https://www.cisco.com/site/us/en/solutions/artificial-intelligence/ai-assistant/index.html), accessed September 7, 2026.

### Cisco Cloud Control

Cisco Cloud Control presents one login, inventory, topology, and operational environment. Its workflow moves from prioritized Actions to AI-assisted investigation and collaborative resolution in AI Canvas. Cisco also positions Cloud Control Studio as the extensibility point for third-party tools and custom agents.

Product implications:

- Evolve the browser from chat-only into inventory, topology, actions, and investigation views.
- Preserve identity, permission, and selected-object context when moving between views.
- Introduce an integration boundary rather than embedding every third-party API in the core.

Source: [Cisco Cloud Control](https://www.cisco.com/site/us/en/solutions/artificial-intelligence/agentic-ops/cisco-cloud-control/index.html), accessed September 7, 2026. Cisco notes that some described capabilities remain in development.

### ThousandEyes assurance in Cloud Control

On August 25, 2026, ThousandEyes announced its integration with Cisco Cloud Control for eligible US1 and US2 organizations using Cisco Unified Identity. The integration surfaces Enterprise Agent inventory, supported alerts in Actions, context-aware access, and supported intelligence through AI Assistant and AI Canvas. Inventory and alert synchronization may take approximately one hour.

The investigation model explicitly asks:

1. Is there a problem?
2. Where is it occurring?
3. What evidence should be investigated next?

It expands fault domains beyond enterprise and WAN infrastructure to ISP, Internet, cloud, SaaS, and application dependencies.

Product implications:

- Add a service-delivery path and fault-domain model.
- Distinguish live, cached, delayed, and unavailable evidence.
- Preserve context handoff to specialized products rather than attempting to replace them.

Source: [Cisco ThousandEyes Comes to Cisco Cloud Control: Assurance Intelligence in the Flow of Operations](https://www.thousandeyes.com/blog/thousandeyes-comes-to-cisco-cloud-control), August 25, 2026.

### ThousandEyes MCP and workflow extension

On September 3, 2026, ThousandEyes described its MCP server as a natural-language path to alerts, tests, metrics, endpoint impact, and path evidence. The demonstrated workflow connects investigation to ServiceNow incident creation and Slack notification.

Product implications:

- Support multiple MCP servers through a connector registry.
- Make evidence portable into incident-management and collaboration workflows.
- Keep workflow actions separate from investigation and require destination-specific approval.

Source: [From Insight to Action: Supercharging Workflows with Cisco ThousandEyes MCP](https://www.thousandeyes.com/blog/from-insight-to-action-supercharging-workflows-with-mcp), September 3, 2026.

### Experience assurance and observability

ThousandEyes' Cisco Live 2025 updates emphasize measuring where experience matters, seeing every part of the path, and contextualizing the source. Announcements included generally available Traffic Insights, mobile assurance in beta, Azure Cloud Insights in private preview, BGP/RPKI and OpenTelemetry improvements, and ThousandEyes/Splunk app-to-network workflows.

Product implications:

- Add trends and traffic context after snapshot persistence exists.
- Track provenance and availability status for every connector.
- Prioritize path, loss, latency, jitter, DNS, and application dependencies over device-only health.

Source: [Assurance Where It Matters Most—Now Powered by AI](https://www.thousandeyes.com/blog/cisco-live-2025), June 10, 2025.

### Agent security

Cisco Secure Access frames agent security around discovering every agent, mapping it to a human owner, issuing short-lived least-privilege authorization for each action, and adapting to runtime risk including prompt-injection-driven exfiltration.

Product implications:

- Assign every future agent and connector a stable identity and owner.
- Keep browser and MCP modes read-only until short-lived, intent-aware authorization exists.
- Record approvals and enforce destination/data policies at runtime.

Source: [Cisco Secure Access](https://www.cisco.com/site/us/en/products/security/secure-access/index.html), accessed September 7, 2026.

## Visual direction

Cisco's public Brand Center protects logo usage and provides an official topology icon library, while detailed internal design-system documentation is access-controlled. This internal Cisco project now packages an approved corporate logo asset and CiscoSansTT webfonts already available in the organization's application assets. It uses:

- Cisco-associated blue and cyan as navigation and interaction accents.
- Green, amber, and red only for operational status.
- The official Cisco vector wordmark rather than a text approximation.
- Self-hosted CiscoSansTT Thin, Light, Regular, Medium, and Bold faces, with Helvetica Neue as a system fallback.
- Compact enterprise navigation, tables, status bands, and evidence views instead of marketing composition.
- An abstract bridge signal rather than the Cisco corporate logo.

Source: [Cisco Brand Center](https://www.cisco.com/c/en/us/about/brand-center.html), accessed September 7, 2026.
