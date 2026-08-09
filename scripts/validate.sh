#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"
# Do not inherit developer credentials (including a loaded .envrc) into checks.
clean_env=(env -i "PATH=$PATH" "HOME=$HOME" "LANG=${LANG:-C}")
"${clean_env[@]}" python3 -m compileall -q demo scripts config
"${clean_env[@]}" bash -n scripts/configure-demo.sh scripts/demo.sh scripts/doctor.sh scripts/validate.sh
"${clean_env[@]}" docker compose -f compose.demo.yaml config >/dev/null
"${clean_env[@]}" python3 -m pytest -q tests/test_public_baseline.py
