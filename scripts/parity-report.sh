#!/usr/bin/env bash
set -euo pipefail
cat <<'EOF'
NexGate parity status
- Offline mock gateway: fixture-passing
- Offline validation: fixture-passing
- Gateway-boundary streaming/tools: fixture-passing
- OpenAI-compatible and Anthropic-compatible adapters: fixture-passing
- LiteLLM gateway, provider/model catalog, and callback modules: migrated
- Claude Code and Codex wiring: migrated; pending operator comparison
- Prometheus and Grafana dashboard: migrated; pending operator telemetry check
- Manual local-overlay diagnostic: available, opt-in only
- Native local runtime: operator-selected OpenAI-compatible endpoint
- Cloud and Tailnet topology: operator-owned overlay
EOF
