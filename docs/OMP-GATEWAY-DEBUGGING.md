# OMP gateway integration

The installed OMP configuration uses the `nexgate` provider namespace. Model
roles previously pointing at `litellm/...` or `litellm-chat/...` are migrated
without changing the selected model or thinking level. Restart OMP after
regenerating the catalog; a running process keeps its loaded configuration.

## Catalog generation

`scripts/configure-omp-gateway.py` reads LiteLLM `/model/info` and installs
explicit model definitions in `~/.omp/agent/models.yml`:

- `anthropic/*` deployments use `anthropic-messages`.
- `chatgpt/*` or deployments with `mode: responses` use `openai-responses`.
- Other chat deployments use `openai-completions`. An `openai/*` backend prefix
  alone does not identify a native Responses endpoint.
- Non-chat models, internal `fallback-*` aliases, and the exhaustion sink are
  excluded from direct selection. Gateway fallback policies still apply.

The provider intentionally leaves `auth` unset. In OMP 18.2.11 this enables
OAuth request shaping for custom `anthropic-messages` models while other APIs
retain their normal request format. Setting `auth: apiKey` suppresses the
Claude system blocks even though the gateway authenticates upstream with
OAuth. The gateway client key remains the credential used to access NexGate.

Context limits, output limits, vision, and reasoning capabilities come from
gateway metadata. Missing limits use conservative defaults of 128,000 input
tokens and 32,768 output tokens. The generated catalog contains capabilities,
not proof that every upstream model currently has capacity or valid credentials.

```sh
.venv/bin/python scripts/configure-omp-gateway.py
omp --model nexgate/glm-5.3
```

First setup needs an existing gateway client key in `NEXGATE_OMP_API_KEY`.
Subsequent runs reuse its protected reference in `~/.omp/agent/.env`. Models
contain the environment variable name rather than the key. Files replaced by
the installer have timestamped, mode-0600 backups. Use `--base-url` for a remote
gateway and `--agent-dir` for a separate client configuration.

## Malformed history protection

`scripts/omp-history-guard.mjs` is installed as
`~/.omp/agent/extensions/nexgate-history-guard.js`, which OMP automatically loads.
Its context hook replaces malformed tool calls with a short diagnostic and
omits their matching tool results from outgoing history. It clears cached
provider payloads on repaired assistant messages. Saved transcripts remain
available for investigation. The guard never guesses a tool name or turns
malformed text into an executable command.

Names must use ASCII letters, digits, underscores, or hyphens and fit the
64-character common limit used by OpenAI function tools. Existing valid calls
from tools no longer enabled in the current session remain intact.

## Observed failures on 2026-10-05

The MacBook used OMP 18.2.11. In session
`01a10b3d-312f-738f-be0c-0eed7c76e301`, GLM 5.3 through `openai-responses`
saved tool names of 54,476, 57,149, 59,758, and 25,944 characters with empty
arguments. The captured Opus request then replayed those names in
`messages[*].tool_calls[*].function.name`. Anthropic rejected the history:

```text
messages.1.content.1.tool_use.name: String should have at most 200 characters
```

The original GLM output is malformed; the investigation did not establish
whether the upstream model or its Responses translation first produced it.
Explicit Chat Completions routing avoids that translation path, and the guard
keeps already-saved malformed calls from poisoning subsequent model switches.

Two authentication/request-format problems were independently verified:

- The gateway selected a fresh Claude Code access token, but its dispatch
  patch discarded injected Authorization and rebuilt it from the stale
  deployment credential. Primary deployments now resolve the live token at
  dispatch; separate fallback deployment credentials remain isolated.
- Explicit `auth: apiKey` in the generated OMP provider disabled its built-in
  Claude OAuth request shaping. With the same fresh token and headers, a tiny
  direct request returned 429 without Claude Code's system blocks and 200
  with them. Removing the override restored successful OMP requests.

These 429 responses did not prove that the working Claude subscription was
exhausted. The separately observed fallback error,
`oauth_not_allowed_for_organization`, indicates an organization OAuth
restriction rather than demonstrated fallback quota exhaustion.

## Credential synchronization

`scripts/sync-claude-oauth.py` mirrors only `accessToken` and `expiresAt` from
the host's Claude Code credentials into the protected
`runtime/state/anthropic/oauth.json`. It does not export the refresh token or
refresh the login independently. The installed host LaunchAgent runs it every
60 seconds so Claude Code's token refreshes reach the gateway.

Set `CCPROXY_ANTHROPIC_OAUTH_TOKEN_FILE=/run/nexgate/anthropic/oauth.json` in the
private environment. Compose mounts the snapshot directory read-only. The
callback rejects expired or unavailable configured snapshots rather than
silently returning to the stale static primary token. A changed snapshot
clears cooldown state inherited from the previous primary credential.

## Verification

Local and MacBook OMP smoke tests passed for GLM Chat Completions, native
ChatGPT Responses, and a GLM read-tool round trip. The history guard processed
the actual four malformed calls and their four results without invalid names
remaining. Opus 5.5 and Fable read-tool round trips passed on both the local
Mac and the bridged MacBook after both fixes. Only representative model paths were
exercised live; the catalog is not a full-model availability certification.

```sh
.venv/bin/pytest -q
bun test tests/omp-history-guard.test.mjs
```

The local OMP install uses a private Bun 1.3.14 runtime through
`~/.local/bin/omp`; the system Bun 1.3.11 could not parse the installed bundle.
The MacBook's existing OMP binary remains installed.
