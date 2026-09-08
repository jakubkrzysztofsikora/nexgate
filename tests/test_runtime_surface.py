from __future__ import annotations

import re
import subprocess
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_portable_litellm_catalog_preserves_the_migration_surface() -> None:
    catalog_text = (ROOT / "runtime/config/litellm.yaml.tmpl").read_text()
    catalog = yaml.safe_load(catalog_text)
    assert len(catalog["model_list"]) == 101
    assert all("model_name" in model and "litellm_params" in model for model in catalog["model_list"])
    assert all(
        not str(model["litellm_params"].get("api_base", "")).startswith(("http://", "https://"))
        for model in catalog["model_list"]
    )
    assert catalog["general_settings"]["database_url"] == "os.environ/DATABASE_URL"


def test_runtime_compose_is_loopback_bound_and_has_optional_observability() -> None:
    compose = yaml.safe_load((ROOT / "compose.nexgate.yaml").read_text())
    services = compose["services"]
    assert {"litellm", "db", "redis", "prometheus", "grafana"} <= services.keys()
    assert services["prometheus"]["profiles"] == ["observability"]
    assert services["grafana"]["profiles"] == ["observability"]
    for service in ("litellm", "prometheus", "grafana"):
        assert all(str(port).startswith("127.0.0.1:") for port in services[service]["ports"])


def test_harness_wiring_uses_portable_defaults() -> None:
    harness = (ROOT / "bin/nexgate").read_text()
    assert "127.0.0.1" in harness
    assert "tail5d39b4" not in harness
    assert "bin/nexgate restore" in (ROOT / "docs/HARNESS-INTEGRATION.md").read_text()


def test_subscription_routes_are_explicitly_disabled_in_the_example_overlay() -> None:
    example = (ROOT / ".env.nexgate.example").read_text()
    assert "NEXGATE_ENABLE_SUBSCRIPTION_ROUTES=false" in example
    assert "runtime/state/chatgpt" in (ROOT / "compose.nexgate.yaml").read_text()


def test_provider_matrix_documents_every_catalog_alias() -> None:
    """The matrix is the operator-facing contract for what each route costs to
    enable; a route missing from it is a route nobody knows how to turn on."""
    catalog = yaml.safe_load((ROOT / "runtime/config/litellm.yaml.tmpl").read_text())
    documented = set(
        re.findall(r"^\| `([^`]+)` \| ", (ROOT / "docs/PROVIDER-MATRIX.md").read_text(), re.M)
    )
    aliases = {model["model_name"] for model in catalog["model_list"]}
    assert aliases == documented, (
        f"undocumented: {sorted(aliases - documented)}; stale: {sorted(documented - aliases)}"
    )


def test_every_fallback_target_resolves_to_a_defined_model() -> None:
    """A fallback chain naming a model that does not exist is a dead hop: the
    router skips it at the moment the primary is already failing."""
    catalog = yaml.safe_load((ROOT / "runtime/config/litellm.yaml.tmpl").read_text())
    defined = {model["model_name"] for model in catalog["model_list"]}
    router = catalog.get("router_settings", {})

    targets: set[str] = set()
    for key in ("fallbacks", "context_window_fallbacks", "content_policy_fallbacks"):
        for entry in router.get(key) or []:
            for chain in entry.values():
                targets.update(chain or [])
    targets.update(router.get("default_fallbacks") or [])

    assert targets <= defined, f"undefined fallback targets: {sorted(targets - defined)}"
    aliases = router.get("model_group_alias") or {}
    assert set(aliases.values()) <= defined, (
        f"undefined alias targets: {sorted(set(aliases.values()) - defined)}"
    )


def test_local_bielik_hook_is_inert_until_the_operator_opts_in(tmp_path: Path) -> None:
    """An operator who never set NEXGATE_BIELIK_API_BASE must not have weights
    downloaded or a llama-server started on their behalf by `make up`."""
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    for tool in ("hf", "tmux", "llama-server"):
        stub = fakebin / tool
        stub.write_text(f'#!/usr/bin/env bash\necho "INVOKED {tool} $*"\nexit 0\n')
        stub.chmod(0o755)

    env_file = tmp_path / "env"
    env_file.write_text("NEXGATE_BIELIK_API_BASE=https://replace-with-your-endpoint\n")

    result = subprocess.run(
        ["bash", str(ROOT / "scripts/local-bielik.sh")],
        env={
            "PATH": f"{fakebin}:/usr/bin:/bin",
            "HOME": str(tmp_path),
            "NEXGATE_ENV_FILE": str(env_file),
            "NEXGATE_LOCAL_STATE_DIR": str(tmp_path / "state"),
            "NEXGATE_LOCAL_LOG_DIR": str(tmp_path / "logs"),
            "NEXGATE_CHATGPT_STATE_DIR": str(tmp_path / "chatgpt"),
        },
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "INVOKED" not in result.stdout
    assert "skipping local server" in result.stdout
