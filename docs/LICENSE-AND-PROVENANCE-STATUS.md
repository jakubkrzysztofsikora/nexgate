# License And Provenance

This repository is licensed under the [MIT License](../LICENSE). The included
source is a clean-root public baseline: it does not import prior repository
history, credentials, database archives, personal configuration, private
endpoints, LFS objects, Gitlinks, or operational overlays.

## Included-source Record

| Path group | Origin | Review state | Publication condition |
| --- | --- | --- | --- |
| `gateway/`, `demo/`, `scripts/`, `tests/` | New public baseline | Reviewed for public fixtures and no private configuration | MIT license and independent clean-clone check |
| `config/provider-catalog.yaml` | New public catalog | Providers disabled by default | Operator data-routing documentation retained |
| `docs/`, repository metadata | New public documentation | Reviewed for public-release language | Keep release information current |
| `uv.lock` | Resolved development tooling | Dependency policy applies | License/notice review against MIT |

This record is a compatibility and provenance aid, not legal advice. Every
release must regenerate and review its SBOM and source checksum.
