# Test Plan: NexGate gateway routing & runtime (routing fixes, ccshims split, minimax-m3-flash, OMP wiring)

**Ticket**: none (personal repo; tracked via docs/plans/2026-10-23-nexgate-routing-and-architecture.md + `.superpowers/sdd/2026-10-23-nexgate-routing-and-architecture/progress.md`)
**Date**: 2026-10-24
**Tester**: to be assigned

## Scope

In scope — the gateway surface changed between `120909c` and `b72cbbe`:

- Routing config: model groups, `model_group_alias`, per-model `fallbacks`, `context_window_fallbacks`, `default_fallbacks`, router settings (146 groups / 64 aliases / 73 fallback rules in rendered `runtime/state/litellm.yaml`).
- New route: `minimax-m3-flash` → `openai/MiniMax-M3.1-Flash` (1M ctx).
- Runtime split: `ccproxy_callback.py` facade + `ccshims/` package (state owners: `oauth`, `const`, `tele`).
- Cache-control hook dual-compat fix (`b72cbbe`): wrapper forwards only params the original hook accepts (litellm 1.95 baseline + 1.100 candidate).
- OMP client wiring on both machines (Mac Studio + jakubs-macbook-pro): 209-model catalog, preserved roles, tailnet gateway URL.
- Provider status probes and docs parity (PROVIDER-MATRIX rows, runtime-surface catalog pin).

Out of scope:

- Research-agent A2A service internals beyond the spike suite (separate suite, `NEXGATE_RUN_A2A_SPIKE=1`).
- Upstream provider SLAs (Anthropic/MiniMax/Qwen cloud availability themselves).
- `sitecustomize.py` split (Task 7 of the plan — deliberately not split; only the cache-control hook fix is in scope).
- Cost/budget enforcement correctness beyond `max_budget` presence.

## Test Environment

- Gateway: `docker compose -f compose.nexgate.yaml --env-file .env.nexgate up -d` → `http://127.0.0.1:4000` (health: `/health/liveliness`).
- Auth: `LITELLM_MASTER_KEY` from `.env.nexgate` (note: `sk-nexgate-local` is NOT a valid token).
- Config truth: `runtime/config/litellm.yaml.tmpl` → `python3 scripts/render-litellm-config.py` → `runtime/state/litellm.yaml` (hand-edits to the rendered file are wiped).
- Second machine: `jakubs-macbook-pro.tail5d39b4.ts.net`, gateway alias `http://litellm.tail5d39b4.ts.net/v1`, OMP dir `~/.omp/agent/`.
- Baseline container for rollback parity: image `a2a-rollback-1.95.0` (litellm 1.95.0, built from `b7eda5c`).
- Test data: rendered config from current template; no seeded conversations required.

## Test Scenarios

### A. Routing — groups & aliases

### TC-001: Bare model group resolves [P1]
**Category**: Functional
**Preconditions**: Gateway healthy; master key available.
**Steps**:
1. `POST /v1/chat/completions` with `model: "minimax-m3-flash"`, `max_tokens: 32`.
2. Inspect `model` field in response.
**Expected**: 200; `model` == `minimax-m3-flash`; non-empty content or reasoning content.

### TC-002: Alias maps to canonical group [P1]
**Category**: Functional
**Steps**:
1. Call with `model: "gemma-2-27b-it"` (alias → `gemma`).
2. Call with `model: "opus[1m]"` (alias → `claude-opus-4-8[1m]`).
3. Call with `model: "qwen38-27b"` (alias → `qwen38-mlx-local`).
**Expected**: each resolves to the mapped group (verify via response `model` + gateway logs), no "unknown model" error.

### TC-003: Provider-prefixed group pins upstream [P1]
**Category**: Functional
**Steps**:
1. Call `opencode/glm-5.3` and `qwencloud/qwen3.8-max`.
**Expected**: requests hit the pinned upstream (check deployment logs), not the bare group's provider.

### TC-004: Unknown model returns clean error [P1]
**Category**: Error handling
**Steps**:
1. `POST /v1/chat/completions` with `model: "does-not-exist"`.
**Expected**: 400-class error with model-not-found message; no 500; gateway stays healthy.

### TC-005: Invalid auth rejected [P1]
**Category**: Error handling
**Steps**:
1. Call `/v1/chat/completions` with `Authorization: Bearer sk-nexgate-local`.
**Expected**: 401; no upstream call made.

### B. Fallback behavior

### TC-010: Free-GPU chain falls through to sink [P1]
**Category**: Functional
**Preconditions**: `qwen36-modal` unreachable or exhausted (stop the Modal app or use a bogus key in a dry-run env).
**Steps**:
1. Call `qwen36-modal`.
2. Observe fallback order in logs: `qwen36-kaggle` → `llm-422-sink`.
**Expected**: final response is the sink's 429 "All free LLM tiers exhausted"; no retry loop; no paid-model attempt.

### TC-011: Claude primary falls back only to secondary credential [P1]
**Category**: Functional
**Preconditions**: primary Claude credential rate-limited (observed live 2026-10-24: upstream 429 on `claude-haiku-4-5-20251001`).
**Steps**:
1. Call a Claude group while primary is limited.
2. Inspect gateway logs for fallback target.
**Expected**: 429 propagates to client unchanged; logs show `Available Model Group Fallbacks=[]` — claude groups are terminal-empty by design (no `fallback-claude-*` groups exist in the rendered config; never routed to kimi/qwen/glm wildcard chain).

### TC-012: Terminal groups never inherit wildcard fallback [P1]
**Category**: Edge case
**Steps**:
1. Force failure on `coopforge/bielik-minitron-7b`, `circit-gateway`, and one terminal-empty claude group (e.g. `claude-haiku-4-5-20251001`).
**Expected**: immediate error to client; logs show no `*`-wildcard fallback attempt (explicit `[]` honored).

### TC-013: Context-window overflow escalates Claude to [1m] [P2]
**Category**: Edge case
**Steps**:
1. Send a prompt larger than the non-`[1m]` context window to `claude-sonnet-5` (or simulate via `context_window_fallbacks` unit path).
**Expected**: reroute to `claude-sonnet-5[1m]`; response succeeds; no generic fallback chain engaged.

### TC-014: Gemma overflow reroutes to minimax/gpt-oss [P2]
**Category**: Edge case
**Steps**:
1. Overflow `gemma` (16384 ctx) with an oversized prompt.
**Expected**: reroute per `context_window_fallbacks`: `minimax-m3`, then `scaleway/gpt-oss`.

### TC-015: default_fallbacks catch-all order [P2]
**Category**: Edge case
**Steps**:
1. Call a group with no explicit fallback rule while its upstream is down.
**Expected**: attempts follow `default_fallbacks` order: kimi-k3 → qwencloud/qwen3.7-plus → glm-5.3-flash → glm-5.3 → mistral/glm-5.2 → minimax-m3 → mistral.

### C. New route — minimax-m3-flash

### TC-020: Route serves completions [P1]
**Category**: Functional
**Steps**:
1. `POST /v1/chat/completions` `model: minimax-m3-flash`, `max_tokens: 64`.
**Expected**: 200; `model` == `minimax-m3-flash`; content present (with ≥64 tokens, reasoning does not starve content).

### TC-021: Route visible in catalog surfaces [P1]
**Category**: Functional
**Steps**:
1. `GET /model/info` — assert `minimax-m3-flash` present with `context_window` 1000000.
2. `GET /v1/models` — assert id listed.
**Expected**: both surfaces list the route; context window matches official docs (1,000,000).

### TC-022: Fallback participation [P2]
**Category**: Functional
**Steps**:
1. Force failure on `minimax-m3` upstream.
**Expected**: `minimax-m3` explicit chain: `kimi-k3`, `mistral/glm-5.2`, `mistral`, `opencode/minimax-m3`, `qwencloud/qwen3.7-plus`.

### TC-023: minimax-m3-flash inherits wildcard fallback [P1]
**Category**: Functional
**Preconditions**: Rendered config has NO explicit rule for `minimax-m3-flash` (verified 2026-10-24: absent from `fallbacks`, `context_window_fallbacks`, `default_fallbacks`).
**Steps**:
1. Force failure on the `minimax-m3-flash` deployment (bogus key in dry-run env).
2. Inspect fallback order in gateway logs.
**Expected**: inherits the `*` wildcard chain: `kimi-k3` → `qwencloud/qwen3.7-plus` → `mistral/glm-5.2` → `minimax-m3` → `mistral` — i.e. flash degrades to full `minimax-m3`, never to itself. `minimax-m3-flash` is not a fallback target of any other group (standalone route).

### TC-024: OMP wiring on both machines [P1]
**Category**: Functional
**Preconditions**: OMP installed on Mac Studio and jakubs-macbook-pro; laptop reaches `litellm.tail5d39b4.ts.net`.
**Steps**:
1. On each machine: parse `~/.omp/agent/models.yml` — assert `nexgate` provider contains `minimax-m3-flash` and 209 models total.
2. On laptop: live completion through the tailnet URL with the machine's `NEXGATE_OMP_API_KEY`.
3. Check `config.yml` `modelRoles.default` still `nexgate/claude-sonnet-5-5[1m]` on laptop; role selections preserved on Studio.
**Expected**: both machines wired; roles untouched; laptop completion returns `model: minimax-m3-flash`.

### D. Runtime split — ccshims

### TC-030: Facade import compatibility [P1]
**Category**: Functional
**Preconditions**: `PYTHONPATH=runtime/config`, litellm importable.
**Steps**:
1. `import ccproxy_callback` standalone (with litellm/ccproxy stubs if needed).
2. Assert re-exported names resolve and `ccshims` submodules import cleanly.
**Expected**: no ImportError/AttributeError; facade ≤ ~272 lines (re-export lists only).

### TC-031: Module state owned by ccshims [P1]
**Category**: Functional
**Steps**:
1. In-process: write `sys.modules["ccshims.oauth"]._ANTHROPIC_FILE_TOKEN_CACHE` (and cooldown fields); read back via the owning module.
2. Repeat for `ccshims.const._SAVINGS_EVENT_QUEUE` and `ccshims.tele` writer/model-map state.
**Expected**: state visible to runtime consumers; writing facade attributes does NOT silently no-op (fixtures use the STATE_OWNERS forwarding map).

### TC-032: Container boots with ccshims mount [P1]
**Category**: Functional
**Steps**:
1. Recreate `litellm` service (`docker compose ... up -d --force-recreate litellm`).
2. `docker compose exec litellm python -c "import ccproxy_callback"` inside the container.
3. Exercise one `/v1/messages` passthrough.
**Expected**: import succeeds (compose mounts `ccshims/:ro`); passthrough returns upstream status (429 rate-limit is a pass — proves hook executed without 500).

### TC-033: Rollback parity on litellm 1.95 baseline [P1]
**Category**: Functional
**Preconditions**: baseline image `a2a-rollback-1.95.0` built from `b7eda5c`.
**Steps**:
1. Run `NEXGATE_RUN_A2A_SPIKE=1 pytest -q tests/integration/test_litellm_a2a_spike.py::test_rollback_preserves_database_and_existing_routes`.
**Expected**: pass — baseline container serves anthropic passthrough without the `api_base` TypeError (regression guard for `b72cbbe`).

### E. Cache-control hook

### TC-040: Hook accepts both calling conventions [P1]
**Category**: Functional
**Steps**:
1. Unit: `pytest -q tests/test_sitecustomize_cache_control.py` (7 tests).
2. Integration: rollback test above (1.95 original signature).
**Expected**: wrapper forwards only params present on the original (`inspect.signature` intersection); no `unexpected keyword argument` on either litellm version.

### TC-041: Leftover injection points consumed on anthropic path [P2]
**Category**: Edge case
**Steps**:
1. Send a request that leaves `cache_control_injection_points` in kwargs on the native Anthropic path.
**Expected**: points popped for `custom_llm_provider in (None, "anthropic")`; debug log `[CACHE_CTRL_LEFTOVER]` only when leftovers remain on other providers.

### F. Config pipeline & docs parity

### TC-050: Render is deterministic and complete [P1]
**Category**: Functional
**Steps**:
1. `python3 scripts/render-litellm-config.py`; diff `runtime/state/litellm.yaml` against committed state.
**Expected**: no diff (renderer output == committed rendered config); 146 groups present.

### TC-051: Runtime surface catalog pin [P1]
**Category**: Functional
**Steps**:
1. `pytest -q tests/test_runtime_surface.py tests/test_nexgate_wiring.py tests/test_omp_gateway_config.py tests/test_provider_status_probes.py`.
**Expected**: green — distinct `model_name` count matches the pinned number in `test_runtime_surface.py`.

### TC-052: Provider matrix documents every alias [P1]
**Category**: Functional
**Steps**:
1. `pytest -q tests/` (full unit suite).
**Expected**: matrix-doc parity test passes — every route alias has a PROVIDER-MATRIX row (new aliases surface one at a time when missed).

### TC-053: Provider status probes hit real containers [P2]
**Category**: Functional
**Steps**:
1. `bin/nexgate status`.
**Expected**: probes report against actual nexgate containers and db role (no phantom endpoints).

### G. Error handling & resilience

### TC-060: Upstream 429 propagates cleanly [P1]
**Category**: Error handling
**Steps**:
1. Call a Claude group while upstream rate-limited (live condition observed 2026-10-24).
**Expected**: client receives litellm `RateLimitError` body with upstream `rate_limit_error` type; HTTP status 4xx; no gateway 500; health endpoint stays 200.

### TC-061: Stream timeout honored [P2]
**Category**: Performance
**Steps**:
1. Streaming call against a slow/hung upstream (dry-run env with delayed fixture).
**Expected**: stream aborted at `stream_timeout: 1800` (router setting); client gets a terminated stream, not a hang.

### TC-062: Cooldowns disabled by design [P2]
**Category**: Edge case
**Steps**:
1. Two consecutive failures on one deployment, then a healthy call to the same group.
**Expected**: `disable_cooldowns: true` means the deployment is retried again (documented behavior — confirm no surprise cooldown lockout).

### TC-063: Concurrent requests keep state consistent [P2]
**Category**: Performance
**Steps**:
1. 20 parallel `/v1/chat/completions` calls mixing `minimax-m3-flash`, `glm-5.3-flash`, and a Claude group.
**Expected**: all requests resolve (success or clean upstream error); ccshims telemetry queue (`ccshims.const._SAVINGS_EVENT_QUEUE`) drains without cross-request corruption; no container restart.

## Coverage Matrix

| Area | Functional P1 | Error P1 | Edge P2 | Perf P3 |
|---|---|---|---|---|
| Groups & aliases | TC-001..003 | TC-004, 005 | — | — |
| Fallbacks | TC-010, 011, 012, 022, 023 | — | TC-013..015 | — |
| minimax-m3-flash + OMP | TC-020, 021, 024 | — | — | — |
| ccshims split | TC-030..033 | — | — | — |
| Cache-control hook | TC-040 | — | TC-041 | — |
| Config pipeline & docs | TC-050..052 | — | TC-053 | — |
| Resilience | — | TC-060 | TC-062 | TC-061, TC-063 (P2) |

## Automation mapping (already green as of 2026-10-24)

- TC-033, TC-040(integration leg): `tests/integration/test_litellm_a2a_spike.py::test_rollback_preserves_database_and_existing_routes` — PASS (140s).
- TC-040(unit leg): `tests/test_sitecustomize_cache_control.py` — 7/7 PASS.
- TC-030/031: `tests/test_ccproxy_oauth_token.py`, `tests/test_ccproxy_provider_specific_headers.py` — PASS.
- TC-051/052: `tests/test_runtime_surface.py`, `test_nexgate_wiring.py`, `test_omp_gateway_config.py`, `test_provider_status_probes.py` — PASS.
- TC-024: verified manually on both machines 2026-10-24 (catalog parse + live laptop completion).
- TC-032/TC-060: verified manually 2026-10-24 (container recreate + live 429 propagation).
- Known-failing (pre-existing, out of scope): `tests/integration/test_research_adapters.py` structured-query model pick (spike-env adapter preference; failed identically before session changes).

## Risks & Assumptions

- Fallback tests that require forcing upstream failure assume a dry-run env with bogus keys per provider; doing this against live credentials burns quota and may hit provider abuse thresholds — prefer dry-run.
- `disable_cooldowns: true` is intentional; TC-062 documents behavior, does not change it.
- The A2A spike suite needs the baseline image from `b7eda5c` (prisma step slow but cached); rebuilding from scratch takes several minutes.
- Rendered-config diff (TC-050) assumes no intentional uncommitted template edits; checkpoint first if dirty.
- Laptop OMP test depends on tailnet connectivity (`litellm.tail5d39b4.ts.net` resolves only inside the tailnet).
- Upstream model availability (MiniMax, Qwen cloud, Mistral) is external; a failure in TC-020/TC-022 may be provider-side — cross-check upstream status before filing.

## Estimated Effort

- Scenarios: 30 (TC-001..TC-063).
- Already automated/verified: 10 (see automation mapping) → remaining manual: 20.
- Estimated time: ~5–6 h manual execution (dry-run env setup ~1 h included); ~3.5–4 h if only the P1 subset (21 scenarios).
