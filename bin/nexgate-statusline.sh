#!/usr/bin/env bash
# NexGate status line for Claude Code.
# Output: colored NEXGATE badge + active model + integrated subscription health & refresh times.
set -euo pipefail

SETTINGS=".claude/settings.local.json"
[[ -f "$SETTINGS" ]] || exit 0

base_url=$(jq -r '.env.ANTHROPIC_BASE_URL // empty' "$SETTINGS" 2>/dev/null) || exit 0
[[ -n "$base_url" ]] || exit 0

model=$(jq -r '.model // "unknown"' "$SETTINGS" 2>/dev/null) || model="unknown"

# Extract scheme/host/port from the URL
scheme="${base_url%%://*}"
host="${base_url#*://}" host="${host%%:*}" host="${host%%/*}"
port="${base_url#*://}" port="${port#*:}" port="${port%%/*}"
if [[ "$port" == "$host" ]]; then
  if [[ "$scheme" == "https" ]]; then
    port=443
  else
    port=80
  fi
fi

# Gateway Health check — 2s timeout
if curl -fsS --max-time 2 "${scheme}://${host}:${port}/health/liveliness" >/dev/null 2>&1; then
  gw_status="✓"
else
  gw_status="✗"
fi

# Fetch provider limits status if script exists
STATUS_SCRIPT="$(dirname "$(readlink -f "$0")")/../scripts/provider_status.py"
sub_summary=""
if [[ -f "$STATUS_SCRIPT" ]]; then
  sub_summary=$(python3 "$STATUS_SCRIPT" --json 2>/dev/null | jq -r '
    to_entries | map(
      .key as $k | .value as $v |
      (if $v.status == "HEALTHY" then "🟢"
       elif $v.status == "HEAVY USE" then "🟡"
       elif $v.status == "RATE LIMITED" then "⚠️"
       else "🔴" end) +
      ($k | split(" ")[0]) +
      (if $v.countdown != "Rolling" and $v.status != "HEALTHY" then "(" + $v.countdown + ")" else "" end)
    ) | join(" ")
  ' 2>/dev/null || true)
fi

if [[ -n "$sub_summary" ]]; then
  printf "NEXGATE %s %s | %s" "$gw_status" "$model" "$sub_summary"
else
  printf "NEXGATE %s %s" "$gw_status" "$model"
fi
