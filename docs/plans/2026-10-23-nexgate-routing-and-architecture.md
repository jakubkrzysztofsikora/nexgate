# Plan: NexGate findings execution (routing research + architecture review)

Spec: docs/PROVIDER-MATRIX.md (routing doctrine) + docs/ARCHITECTURE.md (structure doctrine).
Source findings: multiprovider-routing research (2026-10-23) + architecture review (2026-10-23).
Context: repo carries ~2,452 lines of uncommitted work-in-progress; Task 1 checkpoints it so
task commits stay clean.

Global constraints:
- TDD: every code change test-first, watch RED then GREEN; full `pytest tests/` per task gate.
- No runtime container restart unless a task explicitly verifies it; rendered config is regenerable.
- Endpoints stay env-parametrized (no hardcoded provider URLs).
- Secrets never committed (.env.* ignored). `.serena/` stays untracked.
- Catalog truth: `runtime/config/litellm.yaml.tmpl` (single source for rendering).

## Task 1: Checkpoint baseline commit

Steps:
1. Run `python3 -m pytest tests/ -q` (skip vagrant/installed-only suites if they misfire; record
   result verbatim). Expected: snapshot of suite state; failures belong to pre-existing WIP and
   get named in the ledger, not fixed here.
2. Stage everything except `.serena/` and commit `chore(gateway): checkpoint work-in-progress
   before architecture task run` with body listing the included areas. Expected: `git status`
   shows only `.serena/` untracked afterwards.

## Task 2: Kimi UA honesty (research finding 1)

Kimi Code plan bans client-identifier tampering; tmpl spoofs `User-Agent: claude-code/0.1.0`.

Steps:
1. READ Kimi Code docs for the exact accepted client identification rule before deciding the
   honest value (web, cite in ledger).
2. RED: new test in `tests/test_runtime_surface.py` — every deployment with api_base host
   `api.kimi.com` must NOT carry a `User-Agent` header override claiming `claude-code/0.1.0`
   (honest = no override, or truthful header). Run: fails.
3. GREEN: remove/replace the spoofed UA override on kimi deployments in
   `runtime/config/litellm.yaml.tmpl`.
4. Gate: manual render (`scripts/render-litellm-config.py`) + `pytest tests/` green.
5. Commit `fix(routing): drop spoofed Claude UA from kimi routes`.

Acceptance: render contains no `claude-code/0.1.0` UA; suite green.

## Task 3: Mistral EU-claim truthing (research finding 2)

`region_name: eu` on LiteLLM mistral deployments is inert (global `api.mistral.ai`). EU real
endpoint costs +10% (ruled OUT: no spend increase without operator approval).

Steps:
1. RED: invariant test — any deployment with `region_name: eu` must use an api_base whose host
   is explicitly regional (`api.eu.`-style). Run: fails for mistral global entries.
2. GREEN: drop `region_name: eu` from mistral global deployments (or remove the key where the
   provider ignores it) and correct any EU wording in `docs/PROVIDER-MATRIX.md` mistral rows.
3. Gate: render + `pytest tests/`. Commit `fix(routing): stop claiming EU inference on global mistral`.

Acceptance: no `region_name: eu` without regional endpoint in tmpl; matrix wording matches.

## Task 4: NVIDIA trial-only (research finding 5)

NVIDIA API trial terms: no production use, trains on content.

Steps:
1. Inventory nvidia hosts in tmpl + their presence in router `general`/tier lists and fallbacks.
2. RED: test — no deployment on an nvidia trial host may appear in general router tier lists.
3. GREEN: remove them from tiers (keep standalone route if referenced; label `trial` in matrix).
4. Gate: render + suite. Commit `fix(routing): exclude NVIDIA trial routes from general tiers`.

## Task 5: provider_status wiring (architecture finding 1)

Uncommitted diff already refreshed model lists. Verify it is fixed, else fix.

Steps:
1. Run `python3 -m pytest tests/test_provider_status_probes.py -q` and `bin/nexgate status`
   (read-only) and read the DB/EPM target the scripts hit. Expected: either green probe tests +
   live target (finding resolved; ledger note) or a defect (fix TDD here).

## Task 6: Split ccproxy_callback.py → package (architecture finding 2)

6,280-line god module. Mechanical split, facade kept for import compatibility.

Steps:
1. Inventory responsibilities + importers (`grep -rn "ccproxy_callback"`).
2. Characterization safety: run the ccproxy test files, record green baseline.
3. Create `runtime/config/ccproxy/` package: move cohesive module sections (oauth, headers,
   callbacks, telemetry) one group at a time; `ccproxy_callback.py` becomes a facade
   re-exporting the public names. Suite green after EACH move.
4. Mirror-mount check: compose mounts handle the package dir (verify compose config; if it
   mounts single files, add the dir mount and note in commit body).
5. Gate: full `pytest tests/`. Commit per coherent module group or one final commit if moves
   are interleaved.

Acceptance: `runtime/config/ccproxy_callback.py` < 200 lines (facade); all ccproxy tests green;
package imported by facade only.

## Task 7: Split sitecustomize.py → package (architecture finding 3)

4,148 lines of startup monkey-patching. Same pattern as Task 6; entrypoint must stay a module
importable as `sitecustomize` (package dir with `__init__.py` qualifies).

Steps — same RED-GREEN shape: inventory, characterization run, split into
`runtime/config/sitecustomize/` package (keep `sitecustomize.py` facade importable), suite green
per move, full suite gate, commit `refactor(runtime): split sitecustomize into package`.

Acceptance: `import sitecustomize` smoke passes with PYTHONPATH=runtime/config; suite green;
facade thin.

## Task 8: Document boundaries (architecture findings 4–5)

Doc-only (TDD exception): extend `docs/ARCHITECTURE.md` with:
- test trees (`tests/` CI unit vs `runtime/tests/` container-parity),
- four Python homes (gateway/ lib, research_agent/ spike, scripts/ ops, runtime/config/ injected),
- catalog truth statement (tmpl is the rendering source; `config/provider-catalog.yaml` is the
  portable public catalog, not a second model truth),
- research topic dirs note.
Commit `docs(architecture): document test trees, python homes, catalog truth`.

## Final review

`review-package` over merge-base..HEAD → dispatch `code-reviewer` subagent (most capable model)
with Review Focus: behavioral parity of the two module splits, auth-route URL/UA regressions,
region claim consistency. Fix pass: Critical/Important only, RED→GREEN each. Minors → ledger.
