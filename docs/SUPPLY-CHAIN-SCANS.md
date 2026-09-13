# Supply-chain scan scope

The `Supply-chain scan` workflow blocks newly changed GitHub Action references
and container images unless they use immutable full SHA-256 pins. It materializes
only changed workflow and Docker/Compose files into a temporary scan tree, so
pre-existing repository-wide pinning debt cannot block an unrelated release.
Pushes and pull requests compare against their event base; a manual dispatch
compares the checked-out commit against its parent.

Dependency snapshots still inspect the full repository and upload reports, but
are advisory. Remediate inherited pinning debt in dedicated, separately tested
changes rather than bundling broad image or workflow rewrites into a release.
