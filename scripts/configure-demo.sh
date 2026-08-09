#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
demo_env="$repo_root/.env.demo"

if [[ -e "$demo_env" ]]; then
  echo "$demo_env already exists; refusing to overwrite it." >&2
  exit 0
fi

umask 077
api_key="demo-$(openssl rand -hex 32)"
printf 'DEMO_API_KEY=%s\nDEMO_PORT=4010\n' "$api_key" > "$demo_env"
echo "Created .env.demo with a unique local demo credential."
