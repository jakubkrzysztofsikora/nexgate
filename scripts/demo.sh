#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"
[[ -f .env.demo ]] || { echo "Run make configure first." >&2; exit 1; }

case "${1:-}" in
  up)
    docker compose --env-file .env.demo -f compose.demo.yaml up -d --wait --wait-timeout 45
    echo "Demo endpoint: http://127.0.0.1:$(sed -n 's/^DEMO_PORT=//p' .env.demo)"
    ;;
  down) docker compose --env-file .env.demo -f compose.demo.yaml down ;;
  *) echo "usage: $0 {up|down}" >&2; exit 2 ;;
esac
