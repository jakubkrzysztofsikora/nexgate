# NexGate Architecture

NexGate runs LiteLLM as the gateway boundary. Operator credentials and provider
API bases live only in the ignored `.env` overlay; `scripts/render-litellm-config.py`
selects the compatible model routes and writes the ignored runtime config.

```text
Claude Code ----\
Codex ----------> NexGate LiteLLM + ccproxy callbacks ---> operator providers
Other clients ---/             |                                or local models
                               +--> Postgres / Redis
                               +--> Prometheus --> Grafana (optional)
```

The mounted compatibility modules preserve the established request handling:
Claude-aware compression, tool-call and streaming normalization, token-budget
clamping, provider capability adaptation, and Codex Responses API bridging.

## A2A Compatibility Gate

The candidate image pins LiteLLM `1.100.1` and `a2a-sdk==1.1.2`, retaining
FastAPI `0.139.0`, CCProxy `1.2.0`, and all three mounted compatibility modules.
The ChatGPT-to-Anthropic routing wrapper forwards LiteLLM's optional model
context arguments, supporting both the 1.95 and 1.100 calling conventions.
Agent registration uses `scripts/provision-research-agent.py` and the current
`/a2a/{agent-id-or-name}/.well-known/agent-card.json` discovery endpoint.
Registration alone does not grant a caller an agent-scoped key.

The opt-in integration suite runs actual Docker images, a disposable Postgres
database, and a controlled HTTP agent using the installed SDK's `compat.v0_3`
models. It does not require operator credentials. The SDK's top-level types
are protobuf v1 types; LiteLLM's JSON-RPC implementation uses the compatibility
types for Message, DataPart, Task, artifacts, and errors.

The runtime configuration is produced by the unchanged template and renderer
copied into a disposable directory, using only a synthetic overlay. Redis,
cache settings, logging settings, and callback registration are retained.
Native `chatgpt/*` Responses and the Anthropic-to-Responses bridge use a local
API-base override and expiring synthetic test credentials; no OAuth login or
operator authentication files are used. Negative JSON-RPC cases verify both
the rejection response and absence of upstream requests. Rollback reuses the
virtual key created by the baseline image for actual model requests.

Build the pre-upgrade baseline from the recorded source, then the candidate:

```bash
git show b7eda5c56941e609eecdeb08cd17c9510ded9966:runtime/Dockerfile.litellm \
  | docker build -f - -t nexgate-litellm:a2a-rollback-1.95.0 .
docker build -f runtime/Dockerfile.litellm -t nexgate-litellm:a2a-test .
NEXGATE_RUN_A2A_SPIKE=1 python3 -m pytest -q tests/integration
./scripts/validate.sh
```

Tests use a dedicated bridge with no published ports. Prisma startup may fetch
its npm toolchain, so outbound dependency access is required. Cleanup removes
only the uniquely named test containers, their anonymous database volume, and
their network. The two image tags remain for inspection.

A successful image build or offline suite is not approval to deploy: all real
integration checks, including schema upgrade, caller spend attribution,
Claude/Codex routes, and rollback with retained data, must pass together.

## Profiles

- Default: LiteLLM, Postgres, and Redis, exposed only on loopback.
- `observability`: adds loopback-bound Prometheus and Grafana with the
  provisioned LiteLLM dashboard.
- Local models: supply an OpenAI-compatible API base in `.env`; NexGate does
  not require a particular runtime or hardware stack.

## Harness Wiring

`bin/nexgate install /path/to/project` writes a reversible Claude Code overlay
in the target project and a managed LiteLLM provider block in `~/.codex`. It
uses the endpoint and master key from the local NexGate `.env`. Run
`bin/nexgate restore` to remove only NexGate-managed configuration.
