"""Token-selection policy for the Anthropic OAuth failover path.

Guards the invariant behind the cutover review BLOCKER: the fallback-account
token is only handed to router-internal fallback hops, never chosen by a
client-supplied model name on a direct call.
"""

from __future__ import annotations

import importlib.util
import sys
import threading
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "runtime/config/ccproxy_callback.py"

PRIMARY_TOKEN = "sk-ant-primary-test"
FALLBACK_TOKEN = "sk-ant-fallback-test"


def _install_stub(name: str, **attrs: object) -> types.ModuleType:
    if name in sys.modules:
        return sys.modules[name]
    module = types.ModuleType(name)
    for attr, value in attrs.items():
        setattr(module, attr, value)
    sys.modules[name] = module
    return module


def load_callback_module() -> types.ModuleType:
    litellm_pkg = _install_stub("litellm")
    _install_stub("litellm.litellm_core_utils")
    _install_stub(
        "litellm.litellm_core_utils.get_llm_provider_logic",
        get_llm_provider=lambda *a, **k: (None, None, None, None),
    )
    del litellm_pkg  # only needed to guarantee parent packages exist

    class _FakeConfig:
        oat_sources: dict = {}

        def get_oauth_token(self, _provider: str) -> str | None:
            return None

    ccproxy_pkg = _install_stub("ccproxy")
    _install_stub("ccproxy.config", CCProxyConfig=type("CCProxyConfig", (), {}), get_config=lambda: _FakeConfig())
    _install_stub("ccproxy.handler", CCProxyHandler=type("CCProxyHandler", (), {}))
    del ccproxy_pkg

    spec = importlib.util.spec_from_file_location("ccproxy_callback_under_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def callback(monkeypatch: pytest.MonkeyPatch):
    module = load_callback_module()
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", PRIMARY_TOKEN)
    monkeypatch.setenv("ANTHROPIC_FALLBACK_TOKEN", FALLBACK_TOKEN)
    monkeypatch.setattr(module, "_OAUTH_TOKEN_CACHE", {})
    monkeypatch.setattr(module, "_anthropic_primary_cooldown_until", 0.0)
    return module


class TestFallbackTokenRequiresRouterHop:
    def test_direct_client_call_to_fallback_group_gets_primary_token(self, callback) -> None:
        token = callback._anthropic_oauth_token_for_model(
            "fallback-claude-fable-5-1", "fallback-claude-fable-5-1", fallback_depth=0
        )
        assert token == PRIMARY_TOKEN

    def test_direct_client_call_by_model_name_only_gets_primary_token(self, callback) -> None:
        token = callback._anthropic_oauth_token_for_model(
            "fallback-claude-opus-4-8[1m]", None, fallback_depth=0
        )
        assert token == PRIMARY_TOKEN

    def test_router_fallback_hop_gets_fallback_token(self, callback) -> None:
        token = callback._anthropic_oauth_token_for_model(
            "fallback-claude-fable-5-1", "fallback-claude-fable-5-1", fallback_depth=1
        )
        assert token == FALLBACK_TOKEN

    def test_fallback_hop_without_fallback_env_degrades_to_live_token(self, callback, monkeypatch) -> None:
        monkeypatch.delenv("ANTHROPIC_FALLBACK_TOKEN", raising=False)
        token = callback._anthropic_oauth_token_for_model(
            "fallback-claude-fable-5-1", "fallback-claude-fable-5-1", fallback_depth=2
        )
        assert token == PRIMARY_TOKEN

    def test_primary_group_never_gets_fallback_token(self, callback) -> None:
        token = callback._anthropic_oauth_token_for_model(
            "claude-fable-5-1", "claude-fable-5-1", fallback_depth=3
        )
        assert token == PRIMARY_TOKEN

    def test_unknown_model_gets_live_token(self, callback) -> None:
        token = callback._anthropic_oauth_token_for_model("whatever-model", None, fallback_depth=0)
        assert token == PRIMARY_TOKEN


class TestFallbackDepthExtraction:
    def test_reads_flat_metadata_depth(self, callback) -> None:
        assert callback._fallback_depth_from_metadata({"fallback_depth": 2}) == 2

    def test_reads_nested_litellm_metadata_depth(self, callback) -> None:
        metadata = {"litellm_metadata": {"fallback_depth": 1}}
        assert callback._fallback_depth_from_metadata(metadata) == 1

    def test_missing_or_garbage_depth_is_direct_call(self, callback) -> None:
        assert callback._fallback_depth_from_metadata({}) == 0
        assert callback._fallback_depth_from_metadata(None) == 0
        assert callback._fallback_depth_from_metadata({"fallback_depth": "x"}) == 0


class TestTokenStateThreadSafety:
    def test_concurrent_token_reads_stay_consistent(self, callback) -> None:
        results: list[str] = []
        barrier = threading.Barrier(12)

        def worker() -> None:
            barrier.wait()
            for _ in range(50):
                token = callback._anthropic_oauth_token_for_model(
                    "fallback-claude-fable-5-1", "fallback-claude-fable-5-1", fallback_depth=1
                )
                results.append(token)

        threads = [threading.Thread(target=worker) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert len(results) == 12 * 50
        assert set(results) == {FALLBACK_TOKEN}

    def test_cooldown_arming_is_visible_across_threads(self, callback) -> None:
        class _Response:
            status_code = 429

        kwargs = {"model": "claude-opus-4-8", "metadata": {"model_group": "claude-opus-4-8"}}
        callback._maybe_arm_anthropic_cooldown(kwargs, _Response())
        assert callback._anthropic_primary_cooldown_until > 0.0
