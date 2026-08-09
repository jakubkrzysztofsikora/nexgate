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
