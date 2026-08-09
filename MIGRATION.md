# Scope And Migration

Only newly authored public-baseline files and individually reviewed source may
enter this repository. Personal operational worktrees and their Git histories
are never copied wholesale.

| Area | Status | Rule |
| --- | --- | --- |
| Offline demo and validation | Present | Must work with no provider account. |
| LiteLLM gateway | Migrated | Loopback default; operator credentials activate only selected routes. |
| Provider and model surface | Migrated | 66 aliases retained; literal endpoints replaced by environment variables. |
| ccproxy callback and token optimization | Migrated | Portable code with no private endpoint or credential values. |
| Claude Code and Codex wiring | Migrated | Reversible operator-installed overlays target the configured gateway. |
| Prometheus and Grafana | Migrated | Optional Compose profile with provisioned dashboard. |
| Local/native runtime | Portable connector | Any operator-run OpenAI-compatible local server can be configured. |
| Cloud and Tailnet topology | Excluded | Operators may configure their own remote endpoint overlay. |
| Archives, logs, state, credentials | Excluded | Never copy or commit. |
| Gitlinks and vendored trees | Excluded | Require provenance/license approval. |

## Target Functional-Parity Definition

Parity is behavior, not copied personal configuration. NexGate migrates the
runtime, model aliases, harness integration, optimization modules, and
observability assets while leaving credentials, private endpoints, host paths,
database archives, account sessions, and historical telemetry out of the
repository. The final acceptance step is an operator-run side-by-side test
against the established stack using their private overlay.

Do not add credentials, private history, LFS objects, Gitlinks, or personal
configuration. New adapter families require provenance review, sanitized
fixtures, and an opt-in local diagnostic before they are documented here.
