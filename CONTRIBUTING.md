# Contributing

## Setup

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

Optional integrations are installed with `.[ai]` or `.[marketplace]`.

## Required checks

Run the same gates as CI before opening a pull request:

```bash
make check PYTHON=.venv/bin/python
make security PYTHON=.venv/bin/python
make build PYTHON=.venv/bin/python
```

Changes to a tool, client behavior, diagnostic rule, installer path, credential handling, or external egress must include focused tests. Keep tools read-only unless a separately reviewed write-capability design is approved.

## Pull requests

Use conventional commits and a focused branch such as `fix/tls-validation` or `feature/device-filter`. Describe user-visible behavior, security/privacy implications, validation evidence, and any vManage versions tested. Never include live credentials, cookies, configurations, customer identifiers, or unsanitized API payloads.
