# Functional-Parity Gates

The release lead owns this checklist. Each item must be fixture-passing before
an adapter may be tried with an operator-owned local overlay, and no overlay
or credential may be committed.

| Capability | Fixture acceptance command | Local-overlay exit criterion | Status |
| --- | --- | --- | --- |
| Mock chat completion | `make demo-smoke` | Not applicable | Passing |
| OpenAI streaming | `uv run pytest tests/contract/test_streaming.py` | Response framing matches fixture | Not started |
| OpenAI tools | `uv run pytest tests/contract/test_tools.py` | Tool request/result protocol matches fixture | Not started |
| Generic OpenAI adapter | `make adapter-contract` | Operator endpoint passes bounded diagnostic | Fixture-passing |
| Anthropic-compatible adapter | `make adapter-contract` | Operator endpoint passes bounded diagnostic | Fixture-passing |
| Local overlay selection | `uv run pytest tests/test_config.py` | Explicit provider selection builds only approved adapters | Fixture-passing |
| Local runtime | `make native-smoke` | Loopback health and request test pass | Not started |

Every newly added acceptance command must remain no-secret by default. Commands
that contact a provider are separate, manual diagnostics with an explicit
budget and timeout.
