# Guarded Change Threat Model

This document covers the read-only operations and v2 planning control planes. The shipped package remains read-only: it can create, approve, and verify change plans, but it contains no vManage change executor. The `/api/v1/change-plans/{id}/apply` route always fails closed with HTTP 403.

## Protected assets

- vManage credentials, sessions, XSRF tokens, and private CA material.
- Device configuration and policy content.
- Investigation, snapshot, workflow, identity, and change-plan records.
- Human approvals and immutable plan hashes.
- Connector and AI-provider credentials.

## Trust boundaries

1. Local browser mode accepts only loopback clients and runs as the fixed `local-owner` identity.
2. Enterprise browser mode is deployed once per customer tenant behind an identity-aware HTTPS proxy. The application validates the forwarded bearer JWT independently and does not trust identity headers.
3. The immediate proxy peer is trusted only when its IP matches an explicit address or CIDR. Forwarded HTTPS from other peers is ignored.
4. vManage is an external read-only evidence source. Public application operations use GET; POST remains limited to authentication.
5. Local persistence is owner-only and encrypts document payloads with per-store keys.
6. AI providers, MCP connectors, HTTPS evidence feeds, and OTLP collectors are optional external egress boundaries.
7. A future write-capable deployment is outside the current trust boundary.

## Threats and controls

| Threat | Current control | Remaining gate |
|---|---|---|
| Actor spoofing | Loopback request enforcement or OIDC signature, issuer, audience, expiry, role, and tenant validation; identity headers are ignored | Customer IdP mapping and penetration test |
| Cross-tenant access | One tenant per enterprise instance; OIDC tenant pinning; governance and change-plan tenant checks | Independent tenant-isolation review before any shared-instance model |
| Host/proxy spoofing | Public host allowlist; forwarded HTTPS accepted only from configured proxy IPs/CIDRs | Customer ingress-path validation |
| Signing-key amplification | JWKS cache, bounded document size, and throttled unknown-key refresh | IdP rate-limit and outage exercise |
| Cross-site mutation | Bearer-only application authentication plus Origin and Fetch Metadata rejection | A synchronizer-token design is mandatory before any cookie auth |
| Plan tampering | Canonical SHA-256 plan hash; encrypted immutable plan content | Signed approval artifact in write-capable deployment |
| Approval replay | Approval binds actor, plan hash, and expiry | Short-lived external authorization token and replay cache |
| Excessive blast radius | Explicit target list and canary subset | Staged executor enforcement and failure injection |
| Stale evidence | Pre-snapshot binding, source freshness, revision conflicts, post-snapshot comparison | Environment-specific freshness thresholds |
| Secret disclosure | Redaction before persistence/egress; configuration hashes only | Destination-specific privacy review |
| Connector compromise | HTTPS, timeout isolation, provenance rewriting, allowlists, classification ceilings | Credential rotation and connector security review |
| Unsafe network mutation | No executor; no vManage write method; Apply always returns 403 | Separate deployment, credentials, allowlist, and independent review |
| Rollback failure | Deterministic rollback recommendation and preserved strategy | Tested rollback executor and operator runbook |

## Execution enablement gates

Live execution must not be added to this package until all of the following are complete:

1. Separate write-capable deployment mode and credentials with least-privilege vManage roles.
2. Enterprise authentication, authorization, CSRF posture, TLS termination, and tenant isolation independently accepted in the target environment.
3. Short-lived authorization bound to actor, intent, target set, operation, and exact plan hash.
4. Server-side operation allowlist limited initially to one approved template or policy workflow.
5. Enforced canary stage, bounded blast radius, timeout, idempotency, and replay protection.
6. Pre-change snapshot freshness gate and post-change verification gate.
7. Tested rollback execution in staging, including injected partial failure and loss of connectivity.
8. Immutable external audit retention and independent security review.

Until those gates are met, approvals are local planning records only and never authorize a network mutation.