#!/usr/bin/env bash
# Local-only pre-NexGate hook: refresh the host-managed subscription state the
# containerised gateway cannot produce itself, then ensure the host llama.cpp
# Bielik server is up before the NexGate stack starts. Mirrors
# sovereign-agent-setup start-local.sh (ChatGPT auth flatten) and
# config/llama-server.local.sh (bielik block). Never commit secrets here.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

env_file="${NEXGATE_ENV_FILE:-.env.nexgate}"
if [[ -f "$env_file" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$env_file"
  set +a
fi

state_dir="${NEXGATE_LOCAL_STATE_DIR:-.llm-stack}"
log_dir="${NEXGATE_LOCAL_LOG_DIR:-logs}"
mkdir -p "$state_dir" "$log_dir"

# ── ChatGPT (Codex) subscription auth ────────────────────────────
# Codex CLI nests tokens under .tokens in ~/.codex/auth.json; LiteLLM's ChatGPT
# provider expects a flat access_token file at CHATGPT_TOKEN_DIR/CHATGPT_AUTH_FILE.
# Flatten into the compose-mounted runtime/state/chatgpt so subscription routes
# authenticate without an in-container device-code login (blocked by Cloudflare).
codex_auth="${CODEX_HOST_AUTH_FILE:-${HOME}/.codex/auth.json}"
chatgpt_state="${NEXGATE_CHATGPT_STATE_DIR:-runtime/state/chatgpt}"
if [[ -f "$codex_auth" ]]; then
  mkdir -p "$chatgpt_state"
  if python3 - "$codex_auth" "$chatgpt_state/auth.json" <<'PY'
import base64, json, os, sys
src, dst = sys.argv[1], sys.argv[2]
with open(src) as f:
    d = json.load(f)
t = d.get("tokens", {}) if isinstance(d, dict) else {}
at = t.get("access_token") or d.get("access_token") or ""
exp = d.get("expires_at")
if exp is None and at.count(".") == 2:
    try:
        p = at.split(".")[1]
        p += "=" * (-len(p) % 4)
        exp = int(json.loads(base64.urlsafe_b64decode(p).decode()).get("exp", 0))
    except Exception:
        exp = 0
flat = {
    "access_token": at,
    "refresh_token": t.get("refresh_token") or d.get("refresh_token") or "",
    "id_token": t.get("id_token") or d.get("id_token") or "",
    "account_id": t.get("account_id") or d.get("account_id") or "",
    "expires_at": exp or 0,
}
os.makedirs(os.path.dirname(dst), exist_ok=True)
with open(dst, "w") as f:
    json.dump(flat, f)
os.chmod(dst, 0o600)
sys.exit(0 if at else 3)
PY
  then
    echo "→ ChatGPT auth flattened → $chatgpt_state/auth.json"
  else
    echo "warning: ChatGPT auth flatten produced no access_token; subscription routes may 401" >&2
  fi
else
  echo "warning: $codex_auth missing; ChatGPT subscription routes will not authenticate" >&2
fi

# Gate on the same signal render-litellm-config.py uses to render the route, so
# an unconfigured operator is never made to download several GB of weights.
case "${NEXGATE_BIELIK_API_BASE:-}" in ""|*replace-with-*)
  echo "→ bielik not configured (NEXGATE_BIELIK_API_BASE unset); skipping local server"; exit 0 ;;
esac

bielik_server_host="${BIELIK_SERVER_HOST:-127.0.0.1}"
bielik_server_port="${BIELIK_SERVER_PORT:-8082}"
bielik_model_repo="${BIELIK_REPO:-${BIELIK_MODEL_REPO:-speakleash/Bielik-11B-v2.3-Instruct-GGUF}}"
bielik_model_file="${BIELIK_MODEL_FILE:-Bielik-11B-v2.3-Instruct-Q4_K_M.gguf}"
bielik_context_length="${BIELIK_CONTEXT_LENGTH:-32768}"
bielik_launchd_managed="${BIELIK_LAUNCHD_MANAGED:-0}"
llama_server_bin="${LLAMA_SERVER_BIN:-llama-server}"

case "$bielik_launchd_managed" in
  0|1) ;;
  *) echo "✗ BIELIK_LAUNCHD_MANAGED must be 0 or 1, got: $bielik_launchd_managed" >&2; exit 1 ;;
esac

if [[ "$bielik_server_host" == "127.0.0.1" || "$bielik_server_host" == "localhost" ]]; then
  bind_host="127.0.0.1"
else
  bind_host="0.0.0.0"
fi

bielik_pid="$state_dir/bielik-server.pid"

is_running() {
  [[ -f "$1" ]] || return 1
  local pid
  pid="$(cat "$1" 2>/dev/null)" || return 1
  kill -0 "$pid" 2>/dev/null
}

bielik_is_running() {
  if [[ "$bielik_launchd_managed" == "1" ]]; then
    curl -fsS "http://${bielik_server_host}:${bielik_server_port}/health" >/dev/null 2>&1
  else
    is_running "$bielik_pid"
  fi
}

if bielik_is_running; then
  if [[ "$bielik_launchd_managed" == "1" ]]; then
    echo "→ bielik-server already running (launchd)"
  else
    echo "→ bielik-server already running (pid $(cat "$bielik_pid"))"
  fi
  exit 0
fi

cache_dir="${LLAMA_CACHE_DIR:-${HOME}/.cache/llama.cpp/models}"
mkdir -p "$cache_dir"
bielik_model_path="$cache_dir/$bielik_model_file"

if [[ "$bielik_launchd_managed" != "1" && ! -f "$bielik_model_path" ]]; then
  echo "→ Downloading $bielik_model_repo/$bielik_model_file …"
  if [[ -n "${HF_TOKEN:-}" ]]; then
    HF_TOKEN="$HF_TOKEN" hf download "$bielik_model_repo" "$bielik_model_file" --local-dir "$cache_dir"
  else
    hf download "$bielik_model_repo" "$bielik_model_file" --local-dir "$cache_dir"
  fi
fi

command -v tmux >/dev/null 2>&1 || {
  echo "✗ tmux is required to keep local llama-server processes detached" >&2
  exit 1
}

launch_tmux_server() {
  local session=$1 pidfile=$2 logfile=$3
  shift 3
  local quoted repo log
  printf -v quoted "%q " "$@"
  printf -v repo "%q" "$repo_root"
  printf -v log "%q" "$logfile"
  tmux kill-session -t "$session" >/dev/null 2>&1 || true
  tmux new-session -d -s "$session" "exec $quoted >> $log 2>&1"
  tmux display-message -p -t "$session" '#{pane_pid}' > "$pidfile"
}

if [[ "$bielik_launchd_managed" == "1" ]]; then
  echo "→ bielik-server managed by launchd (not started here)"
  exit 0
fi

echo "→ Starting bielik-server"
echo "    model: $bielik_model_path"
echo "    ctx:   $bielik_context_length"
echo "    bind:  ${bielik_server_host}:${bielik_server_port}"

launch_tmux_server nexgate-bielik "$bielik_pid" "$log_dir/bielik-server.log" \
  env "LLAMA_CACHE=$cache_dir" "$llama_server_bin" \
  --model "$bielik_model_path" \
  --host "$bind_host" \
  --port "$bielik_server_port" \
  --alias bielik \
  --ctx-size "$bielik_context_length" \
  -b 2048 -ub 2048 \
  -fa on \
  -ngl 99 \
  -t 8 \
  --mlock \
  --prio 3 \
  --parallel 2
echo "    bielik pid: $(cat "$bielik_pid")"

for ((i = 1; i <= 60; i++)); do
  if curl -fsS "http://${bind_host}:${bielik_server_port}/health" >/dev/null 2>&1; then
    echo "  ✓ bielik healthy"
    exit 0
  fi
  if ! is_running "$bielik_pid"; then
    echo "  ✗ bielik-server exited — see $log_dir/bielik-server.log" >&2
    exit 1
  fi
  sleep 2
done
echo "  ✗ timeout waiting for bielik health — see $log_dir/bielik-server.log" >&2
exit 1
