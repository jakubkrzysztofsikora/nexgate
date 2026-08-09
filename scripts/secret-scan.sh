#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

command -v gitleaks >/dev/null 2>&1 || {
  echo "gitleaks is required; install it before running this scan." >&2
  exit 1
}

scan_tree() {
  gitleaks detect --source . --no-git --redact --exit-code 1
}

scan_history() {
  gitleaks git --redact --exit-code 1 --log-opts='--all'
}

scan_archive() {
  local archive_dir
  archive_dir="$(mktemp -d)"
  trap 'rm -rf "$archive_dir"' RETURN
  git archive --format=tar HEAD | tar -xf - -C "$archive_dir"
  gitleaks detect --source "$archive_dir" --no-git --redact --exit-code 1
}

case "${1:-all}" in
  tree) scan_tree ;;
  history) scan_history ;;
  archive) scan_archive ;;
  all) scan_tree; scan_history ;;
  *) echo "usage: $0 {tree|history|archive|all}" >&2; exit 2 ;;
esac
