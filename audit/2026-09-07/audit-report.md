# Cisco vManage MCP Audit Report

Date: 2026-09-07
Version audited: 1.1.0 working tree based on commit `8bc5eae`
Scope: source, tests, package metadata, dependency resolution, stdio MCP protocol, and Cisco DevNet read-only validation

## Executive summary

The project is a read-only Cisco SD-WAN vManage integration with 21 MCP tools, deterministic health/correlation services, a CLI, an interactive console, an installer, and optional AI/telemetry integrations.

The audit found and remediated high-impact issues in TLS defaults, credential exposure, alternate-entry-point diagnosis, fail-open pre-change decisions, audit content leakage, dependency vulnerabilities, marketplace telemetry integration, provider loop bounds, event filtering, and package/version metadata. All 21 tools passed a sanitized live DevNet run after remediation.

Residual risk is **moderate**. Production approval should wait for a production-like scale/soak run, Cisco SD-WAN subject-matter sign-off on correlation thresholds, and a trusted CA chain for the target deployment. The DevNet sandbox required the explicit lab-only TLS exception because its HydrantID issuing chain was not trusted by the available system or bundled CA set.

## Architecture and trust boundaries

1. MCP clients invoke the official Python SDK FastMCP server over stdio.
2. Public tools use `VManageClient.get()` or `get_raw()` only.
3. Authentication uses `POST /j_security_check`, followed by XSRF token retrieval.
4. Python services compute health, impact scope, and root-cause hypotheses.
5. Optional AI providers receive prompts and selected tool results only when a user enables them.
6. Minimal Splunk telemetry and Cisco IDE telemetry are separate, disabled-by-default egress paths.

## Methodology

- Traced all server, CLI, console, provider, installer, service, and tool paths.
- Enumerated runtime MCP schemas and annotations.
- Added unit and contract tests around each confirmed defect.
- Ran Ruff, Pyright, Bandit, runtime dependency audit, branch coverage, build, Twine, pre-commit, and clean-wheel installation checks.
- Exercised MCP initialize/list and all 21 tools against the configured seven-device DevNet sandbox without retaining or printing returned topology/configuration data.
- Compared implementation claims with README, changelog, marketplace metadata, and package artifacts.

## Closed findings

| ID | Severity | Finding | Resolution |
|---|---|---|---|
| AUD-C01 | Critical | Installer exposed vManage credentials in MCP JSON, dry-run/manual output, and Claude process arguments | Credentials now live in an owner-only dotenv file; clients receive only its path |
| AUD-H01 | High | TLS verification defaulted off and invalid values silently disabled it | Verification defaults on; booleans are strict; private CA contexts are supported |
| AUD-H02 | High | CLI, app, and AI console mishandled the diagnosis service tuple | All three entry points now unpack and render the current service contract |
| AUD-H03 | High | Empty or partial evidence could produce healthy status and pre-change `GO` | Incomplete/empty evidence is unknown and pre-change validation fails closed |
| AUD-H04 | High | Resolved runtime dependency set contained known vulnerabilities | Security floors added; isolated runtime audit reports no known vulnerabilities |
| AUD-M01 | Medium | Audit result summaries included device/incident content and only four tools were wrapped | All 21 tools are wrapped; summaries contain type/length only; broader secret redaction added |
| AUD-M02 | Medium | Audit files were non-rotating, could be mode 0644, and setup failures aborted import | Private rotating files, explicit stderr opt-in, and visible nonfatal setup failure implemented |
| AUD-M03 | Medium | Cisco IDE integration used an incompatible middleware API and silently disabled itself | Replaced with the SDK-supported standalone client behind explicit opt-in |
| AUD-M04 | Medium | Claude and OpenAI could loop indefinitely through tool calls | All providers are bounded to ten consecutive tool rounds |
| AUD-M05 | Medium | Live event calls failed because vManage rejects `deviceId` on `/dataservice/event` | Replaced the unsupported parameter with verified GET JSON query rules for time/device filtering and normalized live underscore fields |
| AUD-M06 | Medium | Username-derived telemetry hash was dictionary-reversible | Replaced with a random private installation identifier |
| AUD-M07 | Medium | Interactive console log suppression disabled configured audit file records | Console now removes stream handlers without raising the audit logger threshold |
| AUD-L01 | Low | Version check disabled TLS | Verified HTTPS required |
| AUD-L02 | Low | MCP initialize reported the SDK version rather than package version | Initialize now reports 1.1.0 |
| AUD-L03 | Low | README tool counts, credential, audit, transport, TLS, and telemetry claims drifted | Documentation reconciled with executable behavior |

## Verification results

- Full automated suite: 230 tests passing.
- Core branch coverage: 75.60% with a 60% gate; CLI and installer have explicit 60% per-module gates.
- Optional adapter coverage is enforced separately: `app.py` 80% and `ai_providers.py` 80% branch coverage.
- Optional public Pydantic validation helpers are exported, documented, and covered at 100%.
- Ruff: passing.
- Pyright: 0 errors, 0 warnings.
- Bandit: passing with two narrowly justified fixed-argv installer suppressions.
- Runtime requirements audit: no known vulnerabilities.
- Build/Twine: sdist and wheel pass.
- Clean wheel: 21 tools and all four entry points present; packaged CA bundle present.
- Live DevNet: MCP initialize/list passes and all 21 read-only tools pass.

## Limitations

- DevNet has seven devices and is not a production-scale or production-like staging fabric.
- No load/soak test was run against DevNet to avoid disrupting a shared service.
- No real Anthropic, OpenAI, Google, Splunk, or Cisco IDE event was sent; provider tool cycles and telemetry paths were tested with local fakes and source/SDK inspection.
- Cisco GitHub global marketplace schema search required interactive SSO, so the manifest was JSON-validated but not validated against a marketplace schema.
- Health thresholds and controller/site classification still require operational owner sign-off across supported vManage releases.

## Recommendation

Conditional go for read-only lab and controlled staging use. Production release requires closure or explicit acceptance of AUD-R01, AUD-R02, and AUD-R03 in the remediation backlog. Keep telemetry and AI-provider egress disabled unless separately approved.
