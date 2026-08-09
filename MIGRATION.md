# Clean-Candidate Migration

Only newly authored public-baseline files and individually reviewed source may
enter this repository. The legacy repository is never copied wholesale and its
Git history is never imported.

| Area | Status | Rule |
| --- | --- | --- |
| Offline demo and validation | Present | Must work with no `.env` or account. |
| Provider catalog | Present | Disabled by default; no personal endpoints or policy. |
| Gateway callback source | Pending review | Copy only with fixtures and no account/OAuth behavior. |
| Generic OpenAI-compatible adapter | Fixture-passing | Fake upstream covers tools, SSE, and HTTP errors. |
| Anthropic-compatible adapter | Fixture-passing | Fake upstream covers tools, SSE, and HTTP errors. |
| Other provider adapters | Pending review | One family at a time, fake-upstream tests first. |
| Local/native runtime | Pending review | Explicit opt-in and loopback default. |
| Cloud, Tailnet, observability | Private-only | May become opt-in modules later. |
| Archives, logs, state, credentials | Excluded | Never copy or commit. |
| Gitlinks and vendored trees | Excluded | Require provenance/license approval. |

## Target Functional-Parity Definition

Parity is behavior, not copied personal configuration. The current candidate
implements only authenticated mock chat completion. Streaming, tool protocol,
and adapters are future gates defined in `docs/PARITY-CHECKLIST.md`. A
developer may create an untracked overlay containing their own endpoint and
credentials; it is never needed for `make validate`.

Do not change visibility until credential rotation, clean-history export, LFS
retention/purge, Gitlink provenance, license approval, and independent
clean-clone verification are complete.
