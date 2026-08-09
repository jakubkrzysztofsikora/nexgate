#!/usr/bin/env bash
set -euo pipefail

failures=0
need() {
  if command -v "$1" >/dev/null 2>&1; then
    echo "ok: $1"
  else
    echo "missing: $1" >&2
    failures=1
  fi
}
need git
need docker
need uv
need jq
need openssl
docker compose version >/dev/null 2>&1 || { echo "missing: Docker Compose v2" >&2; failures=1; }
python3 - <<'PY'
import sys
if sys.version_info < (3, 12):
    raise SystemExit("Python 3.12 or newer is required for validation tooling")
print(f"ok: Python {sys.version.split()[0]}")
PY
[[ ! -f .env ]] && echo "ok: no private .env required" || echo "note: .env is intentionally ignored by public commands"
exit "$failures"
