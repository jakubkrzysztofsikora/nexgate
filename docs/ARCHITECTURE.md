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

## Durable Lustro research service

The optional `research` Compose profile adds a private FastAPI A2A service. It
has no host-published port and accepts Agent Card discovery and JSON-RPC calls
only with `LUSTRO_A2A_BEARER_TOKEN`. `message/send` accepts exactly one strict
version-one `LustroResearchSubmission` DataPart containing `request`, trusted
`policy`, and reviewed `seed_records`. The A2A `messageId` must equal the
request ID.

Before any research work is queued, PostgreSQL inserts a task row with a unique
`message_id`. Concurrent sends and retries after a lost acknowledgement read
that row and do not queue another run. `tasks/get` returns durable state;
`tasks/cancel` changes only submitted or working tasks. Completed tasks expose
one `ResearchRun` artifact and never echo the trusted submission envelope.
Submitted tasks are recovered when the service starts. Working tasks carry a
persisted run ID, lease owner, and heartbeat deadline; another instance cannot
claim an active lease. Because a provider or archive side effect may already
have occurred, an expired working lease fails closed for manual reconciliation
instead of rerunning research. Owner/run fencing prevents a stale worker from
persisting an artifact. Cancellation updates durable state and directly awaits
the local execution when owned by the receiving instance. If another instance
owns it, that owner's next heartbeat observes the canceled fence and cancels
the in-flight coroutine within one second; conditional completion prevents artifacts after
cancellation in either case.

The profile is intentionally disabled by default. Configure a dedicated
LiteLLM virtual key, private S3-compatible archive bucket, and dedicated inbound
Lustro token before starting it:

```bash
docker compose exec -T db psql -v ON_ERROR_STOP=1 -U nexgate -d nexgate \
  < runtime/migrations/001_research_a2a_tasks.sql
docker compose --profile research up -d research-agent
```

Startup runs a schema preflight only; it never creates or mutates tables. The
migration is additive and safe to reapply. Startup also rejects missing or
placeholder database, inbound bearer, Tavily, dedicated LiteLLM virtual-key,
archive bucket, or HTTPS archive-endpoint configuration before a worker starts.

Starting the container is not authorization to enable Lustro publication or to
run paid research. Cross-repository container, custody, migration, and disabled
feature gates remain required.

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
