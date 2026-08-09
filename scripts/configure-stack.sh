#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

if [[ -e .env ]]; then
  echo ".env already exists; refusing to overwrite it." >&2
  exit 0
fi

umask 077
master_key="sk-nexgate-$(openssl rand -hex 32)"
postgres_password="$(openssl rand -hex 24)"
grafana_password="$(openssl rand -hex 24)"
awk \
  -v master_key="$master_key" \
  -v postgres_password="$postgres_password" \
  -v grafana_password="$grafana_password" \
  'BEGIN { FS=OFS="=" }
   $1 == "LITELLM_MASTER_KEY" { print $1, master_key; next }
   $1 == "POSTGRES_PASSWORD" { print $1, postgres_password; next }
   $1 == "GRAFANA_ADMIN_PASSWORD" { print $1, grafana_password; next }
   { print }' .env.nexgate.example > .env

echo "Created .env. Add credentials and API bases only for providers you intend to use."
