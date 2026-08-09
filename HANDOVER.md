# Candidate Handover

## Authority And Scope

The authoritative publication process is the private legacy repository's
`docs/OPEN-SOURCE-PUBLICATION-PLAN.md`. This candidate is its preferred
new-root clean-export path, not a rewrite of legacy history. Do not copy the
legacy worktree, Git history, credentials, archives, LFS objects, Gitlinks, or
private operational configuration into this repository.

This document is a working handover for the private candidate only. Before a
public release, replace private-repository references with public documents or
remove them.

## Current State

- Repository: private `jakubkrzysztofsikora/hybrid-llm-stack-public-candidate`
- Default branch: `main`
- Candidate history: new root; no imported legacy commits
- Secret scans: Gitleaks found no secrets in tracked content, reachable history,
  or scanned unreachable commits; TruffleHog verified scan found no findings
- Excluded by design: LFS objects, Gitlinks, database archives, local state,
  credentials, personal paths, private endpoints, and legacy Compose
- Baseline: `make doctor`, `make configure`, `make demo`, `make demo-smoke`,
  `make validate`, `make adapter-contract`, and `make parity-report`
- Reviewed fixture adapters: generic OpenAI-compatible and Anthropic-compatible
  adapters, both disabled unless a local untracked overlay explicitly selects
  one

## Verified Commands

```bash
uv sync --locked --group dev
uv run make doctor validate adapter-contract demo-smoke parity-report
gitleaks detect --source . --no-git --redact --exit-code 1
gitleaks git --redact --exit-code 1 --log-opts='--all'
```

The public baseline and adapter fixtures use no provider credentials or live
gateway. Live operator diagnostics are a later opt-in step and must never be a
pull-request requirement.

## Next Work

1. Add required secret prevention automation: Gitleaks CI, `make hooks`, and a
   documented release-time second scanner/archive scan.
2. Add a bounded, manual local-overlay adapter diagnostic. It must redact
   errors, enforce timeouts, and never run in CI.
3. Add gateway-boundary streaming and tool-call contract fixtures, then migrate
   additional reviewed adapters one at a time.
4. Complete the Phase 3 public assets: approved `LICENSE`, third-party notices,
   provenance/compatibility record, changelog/versioning policy, issue/PR
   templates, Dev Container, SBOM/checksum workflow, and dependency policy.
5. Perform the Phase 4 independent clean-clone verification before any
   visibility change.

## External Owner Gates

The legacy repository's historic credential handling is owner-managed and
outside this candidate. The legacy repository must remain private. Publication
of this candidate still requires approved license/provenance, contributor and
scope decisions, release artifact policy, and independent verification as
defined by the original plan.

## Safety Rules

- Keep `PLAN.md` untracked unless its owner explicitly asks otherwise.
- Do not log, commit, or paste secret values, provider responses, or private
  endpoint details.
- Keep all new providers disabled by default and fixture-tested before adding a
  local-overlay path.
- Do not change repository visibility or create a release from this handover.
