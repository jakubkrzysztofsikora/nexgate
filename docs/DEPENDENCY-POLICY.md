# Dependency Policy

The runtime gateway uses only the Python standard library. Development tools
are pinned in `uv.lock` and changes to them require a review of their license,
purpose, integrity, and current maintenance status.

Before adding any direct or transitive dependency:

1. Confirm it is necessary and document the reason in the pull request.
2. Confirm its license is compatible with the MIT license.
3. Pin it through the lockfile and run `make validate`.
4. Regenerate and review the release SBOM with `make release-audit`.
5. Do not add dependencies that transmit prompts, credentials, telemetry, or
   operator configuration without explicit opt-in and documentation.

Dependency updates follow the same review. Vulnerable dependencies receive a
prioritized security update; a release may be delayed while the owner gate is
resolved.
