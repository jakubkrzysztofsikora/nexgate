# Release Handover

## Authority And Scope

This repository is a clean-root public baseline, not a rewritten operational
history. Do not copy personal worktrees, Git history, credentials, archives,
LFS objects, Gitlinks, or private operational configuration into it.

## Current State

- Repository: `jakubkrzysztofsikora/nexgate`
- Default branch: `main`
- History: new root; no imported operational commits
- Secret scans: Gitleaks found no secrets in tracked content, reachable history,
  or scanned unreachable commits; TruffleHog verified scan found no findings
- Excluded by design: LFS objects, Gitlinks, database archives, local state,
  credentials, personal paths, private endpoints, and legacy Compose
- Baseline: `make doctor`, `make configure`, `make demo`, `make demo-smoke`,
  `make validate`, `make adapter-contract`, `make secrets`, and `make parity-report`
- Reviewed fixture adapters: generic OpenAI-compatible and Anthropic-compatible
  adapters, both disabled unless a local untracked overlay explicitly selects
  one

## Verified Commands

```bash
uv sync --locked --group dev
uv run make doctor validate adapter-contract demo-smoke parity-report
make secrets
make release-audit
```

The public baseline and adapter fixtures use no provider credentials or live
gateway. The manual `make adapter-diagnostic` command is a bounded opt-in step
and must never be a pull-request requirement.

## Next Work

1. Review the generated SBOM/checksum evidence and complete the independent
   clean-clone verification before creating a `v0.1.0` tag or changing
   visibility.
2. Migrate further adapters one family at a time, with sanitized streaming and
   tool-call fixtures before any local-overlay path.

## Release Owner Gate

The MIT license, contributor scope, and release policy are approved. Before a
tag or visibility change, an independent release owner must reproduce
validation from a fresh clone and review the SBOM/checksum evidence.

## Safety Rules

- Keep `PLAN.md` untracked unless its owner explicitly asks otherwise.
- Do not log, commit, or paste secret values, provider responses, or private
  endpoint details.
- Keep all new providers disabled by default and fixture-tested before adding a
  local-overlay path.
- Do not change repository visibility or create a release from this handover.
