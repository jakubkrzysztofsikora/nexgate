#!/usr/bin/env bash
# install-heretic-rp.sh — fill in launchd placeholders and load the two jobs.
# Usage: RUNPOD_API_KEY=... RUNPOD_HERETIC_ENDPOINT_ID=... ./scripts/install-heretic-rp.sh
set -euo pipefail
umask 077

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="$HOME/Library/LaunchAgents"
LOGDIR="$HOME/Library/Logs"
KEY="${RUNPOD_API_KEY:?set RUNPOD_API_KEY (console.runpod.io -> Settings -> API Keys)}"
EP="${RUNPOD_HERETIC_ENDPOINT_ID:?set RUNPOD_HERETIC_ENDPOINT_ID}"

mkdir -p "$DEST" "$LOGDIR"

for plist in com.jakub.heretic-rp-gateway com.jakub.runpod-daily-clamp; do
    src="$REPO/scripts/launchd/$plist.plist"
    out="$DEST/$plist.plist"
    sed -e "s|__REPO__|$REPO|g" \
        -e "s|__RUNPOD_API_KEY__|$KEY|g" \
        -e "s|__ENDPOINT_ID__|$EP|g" \
        "$src" > "$out"
    chmod 600 "$out"
    launchctl bootout "gui/$(id -u)/$plist" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$out"
    echo "loaded $plist -> $out"
done

echo
echo "Next:"
echo "  1. Add to .env.nexgate (read by render-litellm-config.py and compose):"
echo "     NEXGATE_QWEN38_HERETIC_RP_API_BASE=http://host.docker.internal:8311/v1"
echo "     NEXGATE_QWEN38_HERETIC_RP_API_KEY=sk-noop"
echo "     (RUNPOD_API_KEY / RUNPOD_HERETIC_ENDPOINT_ID are embedded in the launchd plists above;"
echo "      use host.docker.internal, not 127.0.0.1, because LiteLLM runs inside Docker)"
echo "  2. python3 scripts/render-litellm-config.py"
echo "  3. direnv exec . docker compose -f compose.nexgate.yaml restart litellm"
echo "  4. curl -s localhost:8311/health"
