# Functional-Parity Gates

The release lead owns this checklist. Each item must be fixture-passing before
an adapter may be tried with an operator-owned local overlay, and no overlay
or credential may be committed.

| Capability | Fixture acceptance command | Local-overlay exit criterion | Status |
| --- | --- | --- | --- |
| Mock chat completion | `make demo-smoke` | Not applicable | Passing |
| Gateway-boundary streaming | `make adapter-contract` | Raw SSE framing matches adapter fixtures | Fixture-passing |
| Gateway-boundary tools | `make adapter-contract` | Tool request/result protocol matches adapter fixtures | Fixture-passing |
| Generic OpenAI adapter | `make adapter-contract` | `make adapter-diagnostic` passes manually | Fixture-passing |
| Anthropic-compatible adapter | `make adapter-contract` | `make adapter-diagnostic` passes manually | Fixture-passing |
| Local overlay selection | `uv run pytest tests/test_config.py` | Explicit provider selection builds only approved adapters | Fixture-passing |
| LiteLLM provider/model surface | `make validate` | Operator overlay renders the expected selected aliases | Fixture-passing |
| Claude Code and Codex wiring | `make validate` | Operator side-by-side harness run matches established behavior | Pending operator acceptance |
| Token optimization callbacks | `make validate` | Operator workload comparison preserves token and tool behavior | Pending operator acceptance |
| Observability dashboard | Compose config and JSON validation | Operator dashboard receives their gateway telemetry | Pending operator acceptance |
| Local runtime | `make up` | Loopback health and request test pass | Operator-selected runtime |

Every newly added acceptance command must remain no-secret by default. Commands
that contact a provider are separate, manual diagnostics with an explicit
confirmation and ten-second maximum timeout.
