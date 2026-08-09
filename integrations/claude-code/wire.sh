#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
target_directory="${1:-$PWD}"

exec "$repo_root/bin/nexgate" install "$target_directory"
