# NexGate

[![Validate](https://github.com/jakubkrzysztofsikora/nexgate/actions/workflows/validate.yml/badge.svg)](https://github.com/jakubkrzysztofsikora/nexgate/actions/workflows/validate.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-0f766e.svg)](LICENSE)

NexGate is a portable, self-hosted AI gateway and optimization plane. It
packages the proven LiteLLM, ccproxy, Claude Code, Codex, token-optimization,
and observability layers into an operator-owned stack: add your provider
credentials or local model endpoints, then route your harnesses through one
gateway.

The offline demo and validation path require no provider account or API key.
Live provider use requires the operator's own endpoint and credentials; NexGate
never ships a provider account, endpoint, model-runtime download, or personal
deployment topology.

The project is intentionally conservative: remote providers are disabled by
default, tests use local fake upstreams, redirects never receive credentials,
and manual provider probes are explicit and bounded.

## Start Here

Prerequisites: Docker Desktop or Docker Engine with Compose v2, Python 3.12+
and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --locked --group dev
make doctor
make configure
# Edit .env: add only the provider keys and API bases you intend to use.
make up
```

`make configure` writes an ignored `.env` with unique gateway, database, and
Grafana passwords plus placeholders for every supported provider/model route.
`make up` renders only routes whose required values are configured and exposes
the gateway at `127.0.0.1:4000`. Stop it with `make down`.

For an account-free smoke test, run `make configure-demo`, `make demo`, and
`make demo-smoke`; the mock is loopback-only and never contacts an external
service.

## What Is Included

| Area | What it provides | Default posture |
| --- | --- | --- |
| LiteLLM gateway | Multi-provider model catalog, routing, fallbacks, and Responses API | Localhost only by default |
| Provider/model catalog | 66 portable aliases from the proven stack | Rendered only when required values are supplied |
| Claude Code | Gateway settings overlay, model overrides, ccproxy hooks, and strict-MCP guidance | Operator installs into a chosen project |
| Codex | Managed LiteLLM Responses API provider and reversible user config | Operator installs into their Codex profile |
| Token optimization | Claude-aware compression, request sanitization, tool/search recovery, and compatibility patches | Included in the LiteLLM runtime |
| Observability | Prometheus plus provisioned Grafana LiteLLM dashboard | Opt in with `make observability` |

## Provider Adapters

Copy the generated `.env` values into your own secret manager if preferred,
then add the API keys and API bases for the providers you want to use. NexGate
renders only fully configured routes, so an incomplete provider entry cannot
silently receive traffic. The complete configuration is in
`runtime/config/litellm.yaml.tmpl`; its generated form remains ignored.

Provider use is optional and outside the quickstart. Operators remain
responsible for the endpoint's privacy, retention, cost, and access policies.

## Commands

| Command | Purpose |
| --- | --- |
| `make doctor` | Check the local prerequisites without reading `.env`. |
| `make configure` | Create an ignored operator `.env` with generated local credentials. |
| `make up` / `make down` | Render configured routes and start or stop LiteLLM, Postgres, and Redis. |
| `make observability` | Start the stack with Prometheus and Grafana dashboards. |
| `bin/nexgate install /path/to/project` | Wire a project for Claude Code and the user profile for Codex. |
| `bin/nexgate restore` | Reversibly remove the Claude Code and Codex wiring. |
| `make configure-demo` | Create the isolated credential for the account-free mock demo. |
| `make demo` / `make demo-down` | Start or stop the loopback-only mock gateway. |
| `make demo-smoke` | Verify the authenticated demo contract. |
| `make validate` | Run all offline validation, with a sanitized environment. |
| `make adapter-contract` | Run streaming, tool-call, and error-handling adapter fixtures. |
| `make hooks` | Install the staged Gitleaks secret-prevention hook. |
| `make secrets` | Scan tracked content and reachable Git history. |
| `make release-audit` | Create checksum/SBOM evidence and scan the release archive. |

## Security And Scope

Never commit credentials, provider responses, private endpoints, archives,
logs, or account-linked configuration. The repository provides the portable
runtime and integration logic, not a hosted service or a claim of compatibility
with every provider/version without an operator-run integration check.

Read [SECURITY.md](SECURITY.md) before reporting a vulnerability, and see the
[portable quickstart](docs/PORTABLE-QUICKSTART.md),
[provider and model matrix](docs/PROVIDER-MATRIX.md),
[architecture](docs/ARCHITECTURE.md),
[Claude Code and Codex integration](docs/HARNESS-INTEGRATION.md),
[dependency policy](docs/DEPENDENCY-POLICY.md), and
[release policy](docs/RELEASE-POLICY.md) for the operating model.

## Contributing

Contributions that preserve the offline-first, opt-in design are welcome. See
[CONTRIBUTING.md](CONTRIBUTING.md), [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md),
and the [MIT License](LICENSE).
