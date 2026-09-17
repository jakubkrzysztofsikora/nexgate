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

# NEXGATE_BIND_HOST is meant for one extra trusted address (e.g. a tailnet IP).
# A wildcard would publish the gateway on every interface, so refuse it.
validate_bind_host() {
  local bind_host
  bind_host="${NEXGATE_BIND_HOST:-$(sed -n 's/^NEXGATE_BIND_HOST=//p' "$env_file" | head -n 1)}"
  bind_host="${bind_host%%#*}"                                     # drop trailing comment
  bind_host="$(printf '%s' "$bind_host" | tr -d "\"'[:space:]")"   # strip quotes and spaces
  case "$bind_host" in
    0.0.0.0|::|\[::\]|::0)
      echo "Refusing NEXGATE_BIND_HOST=${bind_host}: bind one specific address (e.g. a tailnet IP) or leave it unset for loopback." >&2
      exit 2
      ;;
  esac
}

compose() {
  NEXGATE_ENV_FILE="$env_file" docker compose "${compose_project_args[@]}" --env-file "$env_file" -f compose.nexgate.yaml "$@"
}

case "${1:-}" in
  up)
    require_env_file
    validate_bind_host
    if [[ "${NEXGATE_SKIP_LOCAL_BIELIK:-0}" != "1" ]]; then
      NEXGATE_ENV_FILE="$env_file" ./scripts/local-bielik.sh || echo "warning: local bielik unavailable; bielik routes will fail until it is up" >&2
    fi
    NEXGATE_ENV_FILE="$env_file" uv run python scripts/render-litellm-config.py
    compose up -d --build --wait --wait-timeout 120
    port="$(sed -n 's/^NEXGATE_PORT=//p' "$env_file" | head -n 1)"
    echo "NexGate API: http://127.0.0.1:${port:-4000}"
    # Echo the client-wiring target when it is not loopback (e.g. a tailnet
    # service address), so the banner matches what `bin/nexgate install` writes.
    litellm_host="${LITELLM_HOST:-$(sed -n 's/^LITELLM_HOST=//p' "$env_file" | head -n 1)}"
    if [[ -n "$litellm_host" && "$litellm_host" != "127.0.0.1" && "$litellm_host" != "localhost" ]]; then
      if [[ "$litellm_host" == *:* && "$litellm_host" != \[* ]]; then
        litellm_host="[${litellm_host}]"  # IPv6 literals need brackets in URLs
      fi
      litellm_scheme="${LITELLM_SCHEME:-$(sed -n 's/^LITELLM_SCHEME=//p' "$env_file" | head -n 1)}"; litellm_scheme="${litellm_scheme:-http}"
      litellm_port="${LITELLM_PORT:-$(sed -n 's/^LITELLM_PORT=//p' "$env_file" | head -n 1)}"; litellm_port="${litellm_port:-4000}"
      if [[ "$litellm_scheme:$litellm_port" == "http:80" || "$litellm_scheme:$litellm_port" == "https:443" ]]; then
        echo "Client wiring target: ${litellm_scheme}://${litellm_host}"
      else
        echo "Client wiring target: ${litellm_scheme}://${litellm_host}:${litellm_port}"
      fi
    fi
    ;;
  down) compose down ;;
  observability)
    require_env_file
    validate_bind_host
    NEXGATE_ENV_FILE="$env_file" uv run python scripts/render-litellm-config.py
    compose --profile observability up -d --build --wait --wait-timeout 120
    ;;
  *) echo "usage: $0 {up|down|observability}" >&2; exit 2 ;;
esac
