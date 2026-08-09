#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"
# Do not inherit developer credentials (including a loaded .envrc) into checks.
clean_env=(env -i "PATH=$PATH" "HOME=$HOME" "LANG=${LANG:-C}")
"${clean_env[@]}" python3 -m compileall -q demo gateway scripts config
"${clean_env[@]}" bash -n scripts/configure-demo.sh scripts/configure-stack.sh scripts/demo.sh scripts/doctor.sh scripts/secret-scan.sh scripts/release-audit.sh scripts/stack.sh scripts/validate.sh
"${clean_env[@]}" docker compose -f compose.demo.yaml config >/dev/null
"${clean_env[@]}" NEXGATE_ENV_FILE=.env.nexgate.example docker compose --env-file .env.nexgate.example -f compose.nexgate.yaml config >/dev/null
"${clean_env[@]}" python3 -m pytest -q tests
