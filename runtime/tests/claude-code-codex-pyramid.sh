#!/usr/bin/env bash
# Pyramid test for Claude Code CLI through LiteLLM proxy using ChatGPT routes.
# Levels: bare prompt → tools → MCP → full settings
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

SKIP_IF_UNAVAILABLE=0
if [[ "${1:-}" == "--skip-if-unavailable" ]]; then
  SKIP_IF_UNAVAILABLE=1
  shift
fi

if ! command -v claude >/dev/null 2>&1; then
  if (( SKIP_IF_UNAVAILABLE )); then
    echo "SKIP: claude CLI is not installed"
    exit 0
  fi
  echo "ERROR: claude CLI is not installed" >&2
  exit 2
fi

# shellcheck disable=SC1091
[[ -f .env ]] && { set -a; source .env; set +a; }

BASE_URL="${ANTHROPIC_BASE_URL:-http://${LITELLM_HOST:-127.0.0.1}:${LITELLM_PORT:-4000}}"
if ! curl -fsS --max-time 5 "${BASE_URL%/}/health/liveness" >/dev/null 2>&1; then
  if (( SKIP_IF_UNAVAILABLE )); then
    echo "SKIP: LiteLLM is not reachable at ${BASE_URL%/}"
    exit 0
  fi
  echo "ERROR: LiteLLM is not reachable at ${BASE_URL%/}" >&2
  exit 2
fi

SMOKE_TIMEOUT_SECONDS="${CLAUDE_CODE_SMOKE_TIMEOUT_SECONDS:-180}"
SMOKE_TIMEOUT_BIN=""
if command -v timeout >/dev/null 2>&1; then
  SMOKE_TIMEOUT_BIN="$(command -v timeout)"
elif command -v gtimeout >/dev/null 2>&1; then
  SMOKE_TIMEOUT_BIN="$(command -v gtimeout)"
fi

PYRAMID_TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/claude-codex-pyramid.XXXXXX")"
DEBUG_LOG="$PYRAMID_TMP_DIR/debug.log"
MCP_EMPTY="$PYRAMID_TMP_DIR/empty-mcp.json"
MCP_TEST="$PYRAMID_TMP_DIR/test-mcp.json"
TEST_MCP_SERVER="$PYRAMID_TMP_DIR/mcp-server.py"
PYRAMID_TOOL_DIR="$(mktemp -d "$REPO_ROOT/.claude-code-pyramid.XXXXXX")"
PYRAMID_TEMP_FILES=()

cleanup() {
  rm -rf "$PYRAMID_TMP_DIR"
  rm -f "${PYRAMID_TEMP_FILES[@]}"
  rmdir "$PYRAMID_TOOL_DIR" 2>/dev/null || true
}
trap cleanup EXIT

# Empty MCP config for levels 1-2
printf '{"mcpServers":{}}\n' > "$MCP_EMPTY"

# Test MCP server for level 3
cat << 'PYEOF' > "$TEST_MCP_SERVER"
#!/usr/bin/env python3
import asyncio, json, sys

async def main():
    init = json.loads(sys.stdin.readline())
    print(json.dumps({"jsonrpc": "2.0", "id": init.get("id"), "result": {
        "protocolVersion": "2024-11-05",
        "capabilities": {"tools": {}, "resources": {}},
        "serverInfo": {"name": "test-server", "version": "1.0.0"}
    }}), flush=True)
    while True:
        line = sys.stdin.readline()
        if not line:
            break
        req = json.loads(line)
        method = req.get("method")
        req_id = req.get("id")
        if method == "tools/list":
            print(json.dumps({"jsonrpc": "2.0", "id": req_id, "result": {"tools": [
                {
                    "name": "test_echo",
                    "description": "Echo back the input",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"message": {"type": "string"}},
                        "required": ["message"]
                    }
                }
            ]}}), flush=True)
        elif method == "resources/list":
            print(json.dumps({"jsonrpc": "2.0", "id": req_id, "result": {"resources": [
                {
                    "uri": "test://health",
                    "name": "test-health",
                    "description": "Dummy resource to keep the server discoverable",
                    "mimeType": "text/plain"
                }
            ]}}), flush=True)
        elif method == "resources/read":
            uri = req.get("params", {}).get("uri", "")
            print(json.dumps({"jsonrpc": "2.0", "id": req_id, "result": {"contents": [
                {
                    "uri": uri,
                    "mimeType": "text/plain",
                    "text": "ok"
                }
            ]}}), flush=True)
        elif method == "tools/call":
            args = req.get("params", {}).get("arguments", {})
            msg = args.get("message", "")
            print(json.dumps({"jsonrpc": "2.0", "id": req_id, "result": {"content": [
                {"type": "text", "text": f"ECHO: {msg}"}
            ]}}), flush=True)
        else:
            print(json.dumps({"jsonrpc": "2.0", "id": req_id, "result": {}}), flush=True)

if __name__ == "__main__":
    asyncio.run(main())
PYEOF
chmod +x "$TEST_MCP_SERVER"

# MCP config with test server
cat << EOF > "$MCP_TEST"
{"mcpServers":{"test-server":{"command":"python3","args":["$TEST_MCP_SERVER"]}}}
EOF

parse_claude_result() {
  if grep -Eiq "Invalid tool parameters|required parameter .* missing|tool_use_error|malformed tool" <<< "$1"; then
    echo "ERROR: Claude Code output contains a tool-parameter failure" >&2
    echo "__TOOL_PARAMETER_ERROR__"
    return 0
  fi
  python3 - "$1" <<'PY'
import json, sys
raw = sys.argv[1]
try:
    data = json.loads(raw)
except json.JSONDecodeError:
    print("")
    raise SystemExit(0)
if data.get("is_error") or data.get("api_error_status"):
    print("")
    raise SystemExit(0)
print((data.get("result") or "").strip())
PY
}

run_claude() {
  local model="$1"
  shift
  local command=(claude)
  if [[ -n "$model" && "$model" != "default" ]]; then
    command+=( -p --model "$model" )
  else
    command+=( -p )
  fi
  command+=( "$@" )
  if [[ -n "$SMOKE_TIMEOUT_BIN" ]]; then
    "$SMOKE_TIMEOUT_BIN" --kill-after=10s "${SMOKE_TIMEOUT_SECONDS}s" "${command[@]}"
  else
    "${command[@]}"
  fi
}

# ---------------------------------------------------------------------------
# Level 1: Bare prompt — no tools, no MCP, no skills
# ---------------------------------------------------------------------------
level1() {
  local model="$1"
  echo "→ [LEVEL 1] Bare prompt (${model:-default})"
  local attempt result status output
  for attempt in 1 2 3; do
    set +e
    output=$(
      printf '%s\n' 'Return exactly PYRAMID_BARE_OK and no other text.' | run_claude "$model" \
        --output-format json \
        --debug-file "$DEBUG_LOG" \
        --mcp-config "$MCP_EMPTY" \
        --strict-mcp-config \
        --setting-sources "" \
        --tools none 2>&1
    )
    status=$?
    set -e
    if (( status == 0 )); then
      result=$(parse_claude_result "$output") || result=""
      if [[ "$result" == "PYRAMID_BARE_OK" ]]; then
        echo "✓ Level 1 bare prompt OK (attempt $attempt)"
        return 0
      fi
    fi
    if (( attempt < 3 )); then sleep 1; fi
  done
  echo "$output" >&2
  echo "FAIL: Level 1 bare prompt failed after 3 attempts for ${model:-default}" >&2
  return 1
}

# ---------------------------------------------------------------------------
# Level 2: Tools — Bash tool allowed
# ---------------------------------------------------------------------------
level2() {
  local model="$1"
  echo "→ [LEVEL 2] With Bash tool (${model:-default})"
  local attempt result status output seed expected source_file state_file state_contents
  for attempt in 1 2 3; do
    seed="CC_PYRAMID_L2_${RANDOM}_${RANDOM}"
    expected="${seed}_DONE"
    source_file="$(mktemp "$PYRAMID_TOOL_DIR/l2-source.XXXXXX")"
    state_file="$(mktemp "$PYRAMID_TOOL_DIR/l2-state.XXXXXX")"
    PYRAMID_TEMP_FILES+=("$source_file" "$state_file")
    printf '%s' "$seed" > "$source_file"
    set +e
    output=$(
      printf '%s\n' "Call the Bash tool with command: cp $source_file $state_file. Do not answer in prose. After that tool result is returned, call the Bash tool with command: cat $state_file. After the second tool result is returned, append _DONE to the exact second tool output and return only that final string." | run_claude "$model" \
        --output-format json \
        --debug-file "$DEBUG_LOG" \
        --mcp-config "$MCP_EMPTY" \
        --strict-mcp-config \
        --setting-sources "" \
        --allowedTools 'Bash(*)' \
        --tools Bash \
        --permission-mode acceptEdits 2>&1
    )
    status=$?
    set -e
    if (( status == 0 )); then
      result=$(parse_claude_result "$output") || result=""
      state_contents="$(<"$state_file")"
      if [[ "$state_contents" == "$seed" && "$result" == *"$expected"* ]]; then
        echo "✓ Level 2 with Bash tool OK (attempt $attempt)"
        return 0
      fi
    fi
    if (( attempt < 3 )); then sleep 1; fi
  done
  echo "$output" >&2
  echo "FAIL: Level 2 Bash tool failed after 3 attempts for ${model:-default}" >&2
  return 1
}

# ---------------------------------------------------------------------------
# Level 3: MCP — test MCP server
# ---------------------------------------------------------------------------
level3() {
  local model="$1"
  echo "→ [LEVEL 3] With MCP server (${model:-default})"
  local attempt result status output
  for attempt in 1 2 3; do
    set +e
    output=$(
      printf '%s\n' 'Use the mcp__test-server__test_echo MCP tool with message "hello from mcp". Return exactly the tool output.' | run_claude "$model" \
        --output-format json \
        --debug-file "$DEBUG_LOG" \
        --mcp-config "$MCP_TEST" \
        --strict-mcp-config \
        --setting-sources "" \
        --tools 'mcp__test-server__test_echo' \
        --allowedTools 'mcp__test-server__test_echo' \
        --permission-mode acceptEdits 2>&1
    )
    status=$?
    set -e
    if (( status == 0 )); then
      result=$(parse_claude_result "$output") || result=""
      if [[ "$result" == *"ECHO: hello from mcp"* ]]; then
        echo "✓ Level 3 with MCP server OK (attempt $attempt)"
        return 0
      fi
    fi
    if (( attempt < 3 )); then sleep 1; fi
  done
  echo "$output" >&2
  echo "FAIL: Level 3 MCP failed after 3 attempts for ${model:-default}" >&2
  return 1
}

# ---------------------------------------------------------------------------
# Level 4: Full tool loop in an isolated Claude Code settings surface.
# Loading the user's global settings would start unrelated MCP servers and
# make a noninteractive transport test nondeterministic.
# ---------------------------------------------------------------------------
level4() {
  local model="$1"
  echo "→ [LEVEL 4] Full isolated settings + Bash loop (${model:-default})"
  local attempt result status output tool_result tool_status tool_output seed expected source_file state_file state_contents
  for attempt in 1 2 3; do
    set +e
    output=$(
      printf '%s\n' 'Reply with exactly OK and no other text.' | run_claude "$model" \
        --output-format json \
        --debug-file "$DEBUG_LOG" \
        --mcp-config "$MCP_EMPTY" \
        --strict-mcp-config \
        --setting-sources "" \
        --tools none \
        --permission-mode acceptEdits 2>&1
    )
    status=$?
    set -e
    if (( status != 0 )); then
      if (( attempt < 3 )); then sleep 1; fi
      continue
    fi

    result=$(parse_claude_result "$output") || result=""
    if [[ "$result" != "OK" ]]; then
      if (( attempt < 3 )); then sleep 1; fi
      continue
    fi

    set +e
    seed="CC_PYRAMID_L4_${RANDOM}_${RANDOM}"
    expected="${seed}_DONE"
    source_file="$(mktemp "$PYRAMID_TOOL_DIR/l4-source.XXXXXX")"
    state_file="$(mktemp "$PYRAMID_TOOL_DIR/l4-state.XXXXXX")"
    PYRAMID_TEMP_FILES+=("$source_file" "$state_file")
    printf '%s' "$seed" > "$source_file"
    tool_output=$(
      printf '%s\n' "Call the Bash tool with command: cp $source_file $state_file. Do not answer in prose. After that tool result is returned, call the Bash tool with command: cat $state_file. After the second tool result is returned, append _DONE to the exact second tool output and return only that final string." | run_claude "$model" \
        --output-format json \
        --debug-file "$DEBUG_LOG" \
        --mcp-config "$MCP_EMPTY" \
        --strict-mcp-config \
        --setting-sources "" \
        --allowedTools 'Bash(*)' \
        --tools Bash \
        --permission-mode acceptEdits 2>&1
    )
    tool_status=$?
    set -e
    if (( tool_status == 0 )); then
      tool_result=$(parse_claude_result "$tool_output") || tool_result=""
      state_contents="$(<"$state_file")"
      if [[ "$state_contents" == "$seed" && "$tool_result" == *"$expected"* ]]; then
        echo "✓ Level 4 isolated settings text+Bash OK (attempt $attempt)"
        return 0
      fi
    fi
    if (( attempt < 3 )); then sleep 1; fi
  done
  echo "$output" >&2
  echo "FAIL: Level 4 full settings failed after 3 attempts for ${model:-default}" >&2
  return 1
}

# ---------------------------------------------------------------------------
# Level 5: reasoning + multiple Bash observations
# ---------------------------------------------------------------------------
level5() {
  local model="$1"
  local seed expected data_file output status result
  seed="CC_REASONING_${RANDOM}_${RANDOM}"
  expected="SUM=15;LINES=5;MARKER=${seed}"
  data_file="$(mktemp "$PYRAMID_TOOL_DIR/reasoning-data.XXXXXX")"
  PYRAMID_TEMP_FILES+=("$data_file")
  printf '%s\n' 1 2 3 4 5 > "$data_file"
  echo "→ [LEVEL 5] Multi-step reasoning + Bash (${model:-default})"

  set +e
  output=$(
    printf '%s\n' "Use Bash exactly twice: first run awk '{sum += \$1} END {print sum}' $data_file, then run wc -l $data_file. Reason over both tool results and return exactly $expected and no other text." | run_claude "$model" \
      --output-format json \
      --debug-file "$DEBUG_LOG" \
      --mcp-config "$MCP_EMPTY" \
      --strict-mcp-config \
      --setting-sources "" \
      --allowedTools 'Bash(*)' \
      --tools Bash \
      --permission-mode acceptEdits 2>&1
  )
  status=$?
  set -e
  if (( status == 0 )); then
    result=$(parse_claude_result "$output") || result=""
    if [[ "$result" == *"$expected"* ]] && [[ "$(grep -c 'tool_dispatch_start tool=Bash' "$DEBUG_LOG" 2>/dev/null || true)" -ge 2 ]]; then
      echo "✓ Level 5 reasoning + Bash OK"
      return 0
    fi
  fi
  echo "$output" >&2
  echo "FAIL: Level 5 reasoning + Bash failed for ${model:-default}" >&2
  return 1
}

# ---------------------------------------------------------------------------
# Level 6: Agent delegation (the child is intentionally tool-free; Levels 2/4/5
# cover built-in tool execution while this level isolates Agent transport).
# ---------------------------------------------------------------------------
level6() {
  local model="$1"
  local agent_name="sovereign-pyramid-agent"
  local child_marker="CC_AGENT_CHILD_${RANDOM}_${RANDOM}"
  local parent_marker="CC_AGENT_PARENT_${RANDOM}_${RANDOM}"
  local agent_config output status result
  agent_config="$(jq -cn --arg name "$agent_name" --arg child "$child_marker" '{
    ($name): {
      description: "Returns an exact marker for the Codex Agent transport test",
      prompt: ("Return exactly " + $child + " and no other text. Do not use tools."),
      tools: [],
      maxTurns: 1
    }
  }')"
  echo "→ [LEVEL 6] Agent delegation (${model:-default})"

  set +e
  output=$(
    printf '%s\n' "Use the Agent tool exactly once with subagent_type $agent_name. After it returns $child_marker, answer exactly $parent_marker:$child_marker and no other text." | run_claude "$model" \
      --output-format json \
      --debug-file "$DEBUG_LOG" \
      --mcp-config "$MCP_EMPTY" \
      --strict-mcp-config \
      --setting-sources "" \
      --tools Agent \
      --allowedTools Agent \
      --agents "$agent_config" \
      --permission-mode acceptEdits 2>&1
  )
  status=$?
  set -e
  if (( status == 0 )); then
    result=$(parse_claude_result "$output")
    if [[ "$result" == *"$parent_marker:$child_marker"* ]]; then
      echo "✓ Level 6 Agent delegation OK"
      return 0
    fi
  fi
  echo "$output" >&2
  echo "FAIL: Level 6 Agent delegation failed for ${model:-default}" >&2
  return 1
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
discover_codex_models() {
  if [[ -n "${CLAUDE_CODE_CODEX_MODELS:-}" ]]; then
    printf '%s\n' "${CLAUDE_CODE_CODEX_MODELS//,/ }"
    return 0
  fi

  # GPT-5.6 aliases are account-dependent on the ChatGPT/Codex backend. Only
  # exercise aliases that LiteLLM actually exposes; an explicit environment
  # override remains available for negative-path testing.
  if [[ -z "${LITELLM_MASTER_KEY:-}" ]]; then
    printf '%s\n' "chatgpt/gpt-5.5"
    return 0
  fi

  python3 - "$BASE_URL" "$LITELLM_MASTER_KEY" <<'PY'
import json
import sys
import urllib.request

base_url, key = sys.argv[1:3]
request = urllib.request.Request(
    f"{base_url.rstrip('/')}/v1/models",
    headers={"Authorization": f"Bearer {key}"},
)
try:
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = json.loads(response.read().decode("utf-8"))
except Exception as exc:
    print(f"ERROR: failed to discover ChatGPT/Codex models: {exc}", file=sys.stderr)
    raise SystemExit(2)

available = {
    item.get("id")
    for item in payload.get("data", [])
    if isinstance(item, dict) and isinstance(item.get("id"), str)
}
candidates = (
    "chatgpt/gpt-5.5",
    "chatgpt/gpt-5.6-sol",
    "chatgpt/gpt-5.6-terra",
    "chatgpt/gpt-5.6-luna",
)
selected = [model for model in candidates if model in available]
if not selected:
    print(
        "ERROR: no configured ChatGPT/Codex GPT-5.5/5.6 model is exposed; "
        "set CLAUDE_CODE_CODEX_MODELS to override",
        file=sys.stderr,
    )
    raise SystemExit(2)
print(" ".join(selected))
PY
}

MODEL_SOURCE="$(discover_codex_models)"
IFS=' ' read -r -a CODEX_MODELS <<< "$MODEL_SOURCE"
FAILED=0

for model in "${CODEX_MODELS[@]}"; do
  echo ""
  echo "═══ Testing pyramid for model: ${model:-default} ═══"
  level1 "$model" || FAILED=$((FAILED + 1))
  level2 "$model" || FAILED=$((FAILED + 1))
  level3 "$model" || FAILED=$((FAILED + 1))
  level4 "$model" || FAILED=$((FAILED + 1))
  level5 "$model" || FAILED=$((FAILED + 1))
  level6 "$model" || FAILED=$((FAILED + 1))
done

echo ""
if (( FAILED == 0 )); then
  echo "═════════════════════════════════════════════════════"
  echo "  ALL LEVELS PASSED for all tested Codex models"
  echo "═════════════════════════════════════════════════════"
  exit 0
else
  echo "═════════════════════════════════════════════════════"
  echo "  $FAILED level(s) FAILED — debug log: $DEBUG_LOG"
  echo "═════════════════════════════════════════════════════"
  exit 1
fi
