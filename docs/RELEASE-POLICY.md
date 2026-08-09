# Release And Versioning Policy

Releases use semantic versioning. The initial public release is `v0.1.0`.

The release lead must complete the following before creating a tag or changing
repository visibility:

1. Confirm the MIT license, provenance record, notices, and release scope.
2. Run `make release-audit` from a clean checkout with `gitleaks` and `syft`.
3. Independently reproduce validation from a fresh clone with no local
   environment files or credential mounts.
4. Review the generated source checksum and SBOM, then record the exact tag
   and commit in the changelog.
5. Publish source artifacts only after the independent verification gate in
   `HANDOVER.md` passes.

The `Release Audit` workflow uploads evidence for review. It does not create a
GitHub release, publish packages, or alter repository visibility.
