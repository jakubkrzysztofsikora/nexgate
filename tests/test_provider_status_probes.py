"""Provider status probes must show TRUE subscription state.

A probe answered by a fallback chain is a false positive: a dead subscription
shows HEALTHY. Probes therefore disable fallbacks per request and only use
model names that exist in the rendered catalog.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]


def _load_script(name: str):
    path = ROOT / f"scripts/{name}.py"
    if not path.exists():
        pytest.fail(f"{path} missing")
    spec = importlib.util.spec_from_file_location(f"{name}_under_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"{name}_under_test"] = module
    spec.loader.exec_module(module)
    return module


def _catalog_model_names() -> set[str]:
    catalog = yaml.safe_load((ROOT / "runtime/state/litellm.yaml").read_text())
    return {m["model_name"] for m in catalog["model_list"]}


@pytest.mark.parametrize("script_name", ["provider_status", "mcp_provider_status"])
class TestProbeTruthfulness:
    def test_probe_payload_disables_fallbacks(self, script_name) -> None:
        module = _load_script(script_name)
        payload = module.build_probe_payload("claude-opus-4-8")
        assert payload["disable_fallbacks"] is True
        assert payload["model"] == "claude-opus-4-8"
        assert payload["max_tokens"] == 1

    def test_probe_models_exist_in_catalog(self, script_name) -> None:
        module = _load_script(script_name)
        names = _catalog_model_names()
        for group in module.PROVIDER_GROUPS.values():
            probe = group["probe_model"]
            assert probe in names, f"{script_name}: stale probe model {probe}"

    def test_probe_model_listed_in_group_models(self, script_name) -> None:
        module = _load_script(script_name)
        for group_name, group in module.PROVIDER_GROUPS.items():
            assert group["probe_model"] in group["models"], group_name

    def test_probe_url_targets_local_gateway(self, script_name) -> None:
        module = _load_script(script_name)
        assert "127.0.0.1" in module.PROBE_URL or "localhost" in module.PROBE_URL

    def test_payment_required_classifies_as_subscription_state(self, script_name) -> None:
        module = _load_script(script_name)
        assert module.classify_probe_failure(402, "Check your subscription") == "QUOTA EXHAUSTED"
        assert module.classify_probe_failure(None, "402 payment required") == "QUOTA EXHAUSTED"

    def test_probe_timeout_allows_slow_subscription_routes(self, script_name) -> None:
        module = _load_script(script_name)
        assert module.PROBE_TIMEOUT_SECONDS >= 20
