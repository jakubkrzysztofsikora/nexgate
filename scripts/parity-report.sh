#!/usr/bin/env bash
set -euo pipefail
cat <<'EOF'
Public candidate parity status
- Offline mock gateway: fixture-passing
- Credential-free validation: fixture-passing
- OpenAI-compatible streaming/tools: not-started
- Provider adapters: not-started
- Native local runtime: not-started
- Cloud, Tailnet, observability: private-only
EOF
