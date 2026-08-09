#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

git diff --quiet && [[ -z "$(git ls-files --others --exclude-standard)" ]] || {
  echo "release audit requires a clean worktree so the archive, checksum, and SBOM match." >&2
  exit 1
}

./scripts/validate.sh
./scripts/secret-scan.sh all
./scripts/secret-scan.sh archive

artifact_dir="reports/release-audit"
rm -rf "$artifact_dir"
mkdir -p "$artifact_dir"
git archive --format=tar.gz --prefix=nexgate/ HEAD > "$artifact_dir/source.tar.gz"
sha256sum "$artifact_dir/source.tar.gz" > "$artifact_dir/source.tar.gz.sha256"

command -v syft >/dev/null 2>&1 || {
  echo "syft is required to generate release SBOM evidence." >&2
  exit 1
}

archive_dir="$(mktemp -d)"
trap 'rm -rf "$archive_dir"' EXIT
tar -xzf "$artifact_dir/source.tar.gz" -C "$archive_dir"
syft "dir:$archive_dir/nexgate" --source-name nexgate --source-version 0.1.0 -o spdx-json > "$artifact_dir/sbom.spdx.json"

echo "release audit passed; artifacts are in $artifact_dir"
