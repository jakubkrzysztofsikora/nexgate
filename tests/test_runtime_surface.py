from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_portable_litellm_catalog_preserves_the_migration_surface() -> None:
    catalog_text = (ROOT / "runtime/config/litellm.yaml.tmpl").read_text()
    catalog = yaml.safe_load(catalog_text)
    assert len(catalog["model_list"]) == 68
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
