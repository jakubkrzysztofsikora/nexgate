#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

env_file="${NEXGATE_ENV_FILE:-.env}"
compose_project_args=()
if [[ -n "${NEXGATE_COMPOSE_PROJECT:-}" ]]; then
  compose_project_args=(--project-name "$NEXGATE_COMPOSE_PROJECT")
fi

require_env_file() {
  [[ -f "$env_file" ]] || {
    echo "No operator overlay at ${env_file} - run make configure first." >&2
    exit 2
  }
}

compose() {
  NEXGATE_ENV_FILE="$env_file" docker compose "${compose_project_args[@]}" --env-file "$env_file" -f compose.nexgate.yaml "$@"
}

case "${1:-}" in
  up)
    require_env_file
    if [[ "${NEXGATE_SKIP_LOCAL_BIELIK:-0}" != "1" ]]; then
      NEXGATE_ENV_FILE="$env_file" ./scripts/local-bielik.sh || echo "warning: local bielik unavailable; bielik routes will fail until it is up" >&2
    fi
    NEXGATE_ENV_FILE="$env_file" uv run python scripts/render-litellm-config.py
    compose up -d --build --wait --wait-timeout 120
    port="$(sed -n 's/^NEXGATE_PORT=//p' "$env_file" | head -n 1)"
    echo "NexGate API: http://127.0.0.1:${port:-4000}"
    ;;
  down) compose down ;;
  observability)
    require_env_file
    NEXGATE_ENV_FILE="$env_file" uv run python scripts/render-litellm-config.py
    compose --profile observability up -d --build --wait --wait-timeout 120
    ;;
  *) echo "usage: $0 {up|down|observability}" >&2; exit 2 ;;
esac
