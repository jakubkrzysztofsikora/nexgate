#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

created_env=0
if [[ ! -f .env.demo ]]; then
  ./scripts/configure-demo.sh
  created_env=1
fi

cleanup() {
  ./scripts/demo.sh down >/dev/null 2>&1 || true
  if [[ "$created_env" == "1" ]]; then
    rm -f .env.demo
  fi
}
trap cleanup EXIT

./scripts/demo.sh up >/dev/null
demo_key="$(sed -n 's/^DEMO_API_KEY=//p' .env.demo)"
port="$(sed -n 's/^DEMO_PORT=//p' .env.demo)"
headers=(-H "Authorization: Bearer $demo_key" -H 'Content-Type: application/json')

curl --fail --silent --show-error "${headers[@]}" "http://127.0.0.1:${port}/v1/models" >/dev/null
response="$(curl --fail --silent --show-error "${headers[@]}" -d '{"model":"demo-echo","messages":[{"role":"user","content":"health"}]}' "http://127.0.0.1:${port}/v1/chat/completions")"
[[ "$response" == *'Demo gateway is healthy.'* ]]
echo "demo smoke passed"
