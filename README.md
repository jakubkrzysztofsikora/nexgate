# NexGate

[![Validate](https://github.com/jakubkrzysztofsikora/nexgate/actions/workflows/validate.yml/badge.svg)](https://github.com/jakubkrzysztofsikora/nexgate/actions/workflows/validate.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-0f766e.svg)](LICENSE)

NexGate is a self-hosted AI gateway: one local endpoint that fronts every
model provider you use, with automatic fallback when a provider dies
mid-session.

[Why](#why) •
[Quickstart](#quickstart) •
[Wire your tools](#wire-your-tools) •
[When a provider fails](#when-a-provider-fails) •
[What is included](#what-is-included) •
[Commands](#commands) •
[Scope](#scope-and-security)

## Why

You use Claude Code, Codex, or any agent CLI. You have more than one model
provider — subscriptions, API keys, a local model. Then this happens:

- A subscription quota runs out mid-task and the session dies.
- Every tool needs its own provider config. They drift. You forget which.
- Switching providers means rewiring every client by hand.
- No single place shows what was spent where.

NexGate replaces all of that with one endpoint on your machine:

- **One URL** // every OpenAI- or Anthropic-compatible client works unchanged
- **Fallback chains** // quota hit or outage transparently retries the next provider
- **Model names as policy** // `claude-opus-4-8[1m]` and friends route to whatever you configured
- **Subscriptions count** // reuse the Claude Code / ChatGPT logins you already pay for
- **Spend in one place** // per-model cost tracking, Prometheus + Grafana dashboards
- **Yours** // no third-party relay sees your traffic; keys never leave your host

## Who this is for

Ideal user: a developer who already pays for two or more AI providers, runs
their own machines (homelab, VPS, or a beefy laptop), and uses agent CLIs
daily. You want provider choice without rewiring your tools, and you'd rather
debug a docker compose stack than open a support ticket.

Ideal use case: point Claude Code, Codex, OpenCode, Cline, or your own scripts
at `http://127.0.0.1:4000`. Pick models by name. When Anthropic rate-limits
your OAuth token at 15:00, the gateway retries the next provider in the chain
and your agent keeps working. You check Grafana to see what it cost.

Not for you if: you want a hosted service, a one-click installer with no
Docker, or a provider account included. NexGate ships the gateway, not the
models or the credentials.

## Quickstart

Prerequisites: Docker with Compose v2, Python 3.12+, [uv](https://docs.astral.sh/uv/).

```bash
uv sync --locked --group dev
make doctor          # check prerequisites, no secrets needed
make configure       # writes an ignored .env with generated local passwords
# Edit .env: add ONLY the provider keys/bases you actually use.
make up
```

The gateway is now on `127.0.0.1:4000`. Providers with missing values are
simply not rendered — an incomplete `.env` cannot receive traffic. Stop with
`make down`.

No provider account at all? The demo runs a loopback-only mock:

```bash
make configure-demo && make demo && make demo-smoke
```

## Wire your tools

| Tool | How |
| --- | --- |
| Claude Code | `bin/nexgate install /path/to/project` writes the gateway overlay (`ANTHROPIC_BASE_URL` + key). `bin/nexgate restore` undoes it. |
| Codex | Same installer wires a LiteLLM Responses-API provider into your Codex profile, reversibly. |
| Anything else | Point it at `http://127.0.0.1:4000` with your `LITELLM_MASTER_KEY`. OpenAI and Anthropic request shapes are both accepted. |

Example — Anthropic shape through the gateway:

```bash
curl http://127.0.0.1:4000/v1/messages \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "anthropic-version: 2023-06-01" \
  -H "content-type: application/json" \
  -d '{"model":"glm-5.3-flash","max_tokens":32,"messages":[{"role":"user","content":"hi"}]}'
```

## When a provider fails

Every model name maps to an ordered fallback chain (see
`runtime/config/litellm.yaml.tmpl`). Example: `claude-fable-5-1` tries your
Anthropic OAuth token first; on a quota error it walks to ChatGPT, then Kimi,
then Qwen, then GLM — and the client just sees a slower response, not an
error. Chains are policy: edit the template, re-render, restart.

## What is included

| Area | What it provides | Default posture |
| --- | --- | --- |
| LiteLLM gateway | Multi-provider model catalog, routing, fallbacks, Responses API | Localhost only |
| Provider catalog | 66 portable aliases; only configured routes render | Opt-in per provider |
| Claude Code / Codex | Wiring overlays, model overrides, ccproxy hooks | Installed by you, reversible |
| Token optimization | Claude-aware compression, request sanitization, tool-call recovery | In the LiteLLM runtime |
| Observability | Prometheus + provisioned Grafana dashboard | `make observability` |

## Commands

| Command | Purpose |
| --- | --- |
| `make doctor` | Check prerequisites without reading `.env`. |
| `make configure` | Create an ignored operator `.env` with generated local credentials. |
| `make up` / `make down` | Render configured routes; start/stop LiteLLM, Postgres, Redis. |
| `make observability` | Start with Prometheus and Grafana dashboards. |
| `bin/nexgate install <project>` | Wire Claude Code + Codex to the gateway. |
| `bin/nexgate restore` | Remove that wiring, reversibly. |
| `make configure-demo` / `make demo` / `make demo-smoke` | Account-free loopback mock and its smoke test. |
| `make validate` | All offline validation, sanitized environment. |
| `make adapter-contract` | Streaming, tool-call, and error-handling adapter fixtures. |
| `make hooks` / `make secrets` | Gitleaks hook install; tracked-content and history scan. |
| `make release-audit` | Checksum/SBOM evidence and release-archive scan. |

## Scope and security

NexGate never ships a provider account, endpoint, model runtime, or personal
deployment topology. Remote providers are disabled by default; tests use local
fake upstreams; redirects never receive credentials; provider probes are
explicit and bounded. Operator remains responsible for each provider's
privacy, retention, cost, and access policy.

Never commit credentials, provider responses, private endpoints, or logs.

Read [SECURITY.md](SECURITY.md) before reporting a vulnerability, and see the
[portable quickstart](docs/PORTABLE-QUICKSTART.md),
[provider matrix](docs/PROVIDER-MATRIX.md),
[architecture](docs/ARCHITECTURE.md),
[harness integration](docs/HARNESS-INTEGRATION.md),
[dependency policy](docs/DEPENDENCY-POLICY.md), and
[release policy](docs/RELEASE-POLICY.md).

## Contributing

Contributions that preserve the offline-first, opt-in design are welcome. See
[CONTRIBUTING.md](CONTRIBUTING.md), [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md),
and the [MIT License](LICENSE).
