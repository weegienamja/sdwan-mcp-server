# Security Policy

## Supported versions

Security fixes are applied to the latest release on `main`.

## Reporting a vulnerability

Report suspected vulnerabilities privately to the repository owner through Cisco's internal security process. Do not open a public issue containing credentials, session tokens, customer topology, running configuration, device identifiers, or exploit details.

Include the affected version, entry point, sanitized reproduction steps, impact, and any relevant vManage version. Rotate any credential that may have been exposed during testing.

## Deployment baseline

- Use a least-privilege vManage operator account.
- Keep `VMANAGE_VERIFY_SSL=true` and configure `VMANAGE_CA_BUNDLE` when a private CA is required.
- Treat `VMANAGE_VERIFY_SSL=false` as an isolated-lab exception only.
- Store vManage credentials through `vmanage-mcp-install` or protected environment injection.
- Enable Splunk or Cisco IDE telemetry only after reviewing the documented payload and destination.
- Do not send production network data to an AI provider without organizational approval.
