"""Token-selection policy for the Anthropic OAuth failover path.

Guards the invariant behind the cutover review BLOCKER: the fallback-account
token is only handed to router-internal fallback hops, never chosen by a
client-supplied model name on a direct call.
"""

from __future__ import annotations

import importlib.util
import json
import threading
import time
import types
from pathlib import Path

import pytest

from conftest import install_stub


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "runtime/config/ccproxy_callback.py"

PRIMARY_TOKEN = "sk-ant-primary-test"
FALLBACK_TOKEN = "sk-ant-fallback-test"


def load_callback_module(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    install_stub(monkeypatch, "litellm")
    install_stub(monkeypatch, "litellm.litellm_core_utils")
    install_stub(
        monkeypatch,
        "litellm.litellm_core_utils.get_llm_provider_logic",
        get_llm_provider=lambda *a, **k: (None, None, None, None),
    )

    class _FakeConfig:
        oat_sources: dict = {}

        def get_oauth_token(self, _provider: str) -> str | None:
            return None

    install_stub(monkeypatch, "ccproxy")
    install_stub(
        monkeypatch,
        "ccproxy.config",
        CCProxyConfig=type("CCProxyConfig", (), {}),
        get_config=lambda: _FakeConfig(),
    )
    install_stub(monkeypatch, "ccproxy.handler", CCProxyHandler=type("CCProxyHandler", (), {}))

    spec = importlib.util.spec_from_file_location("ccproxy_callback_under_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


STATE_OWNERS = {
    "_anthropic_primary_cooldown_until": "oauth",
    "_OAUTH_TOKEN_CACHE": "oauth",
    "_ANTHROPIC_FILE_TOKEN_CACHE": "oauth",
    "_anthropic_token_state_lock": "oauth",
    "_SAVINGS_EVENT_QUEUE": "const",
    "_SAVINGS_EVENT_WRITER_STARTED": "tele",
    "_SAVINGS_EVENT_DROPPED": "tele",
    "_MODEL_NAME_TO_PROVIDER": "tele",
    "_MODEL_NAME_MAP_MTIME": "tele",
}


class _StateForwarding:
    """ccshims modules own their state; the facade re-exports import-time
    copies, so test reads and writes must route to the owning module."""

    def __init__(self, base: object, owners: dict[str, object]) -> None:
        object.__setattr__(self, "_base", base)
        object.__setattr__(self, "_owners", owners)

    def __getattr__(self, name: str) -> object:
        owner = self._owners.get(name)
        if owner is not None:
            return getattr(owner, name)
        return getattr(self._base, name)

    def __setattr__(self, name: str, value: object) -> None:
        owner = self._owners.get(name)
        if owner is not None:
            setattr(owner, name, value)
        else:
            setattr(self._base, name, value)


@pytest.fixture()
def callback(monkeypatch: pytest.MonkeyPatch):
    module = load_callback_module(monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", PRIMARY_TOKEN)
    monkeypatch.setenv("ANTHROPIC_FALLBACK_TOKEN", FALLBACK_TOKEN)
    import ccshims.const
    import ccshims.oauth
    import ccshims.tele
    owners = {name: getattr(ccshims, mod) for name, mod in STATE_OWNERS.items()}
    monkeypatch.setattr(ccshims.oauth, "_OAUTH_TOKEN_CACHE", {})
    monkeypatch.setattr(ccshims.oauth, "_anthropic_primary_cooldown_until", 0.0)
    return _StateForwarding(module, owners)


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


class TestLiveClaudeCodeTokenSnapshot:
    def test_snapshot_overrides_old_environment_and_clears_old_account_cooldown(self, callback, monkeypatch, tmp_path):
        path = tmp_path / "oauth.json"
        path.write_text(json.dumps({"accessToken": "fresh-login", "expiresAt": (time.time() + 3600) * 1000}))
        monkeypatch.setenv("CCPROXY_ANTHROPIC_OAUTH_TOKEN_FILE", str(path))
        monkeypatch.setattr(callback, "_anthropic_primary_cooldown_until", time.monotonic() + 900)
        assert callback._get_live_oauth_token("anthropic") == "fresh-login"
        assert callback._anthropic_primary_cooldown_until == 0
        path.write_text(json.dumps({"accessToken": "refreshed-login", "expiresAt": (time.time() + 3600) * 1000}))
        assert callback._get_live_oauth_token("anthropic") == "refreshed-login"

    def test_expired_snapshot_cannot_silently_reuse_old_environment(self, callback, monkeypatch, tmp_path):
        path = tmp_path / "oauth.json"
        path.write_text(json.dumps({"accessToken": "expired-login", "expiresAt": (time.time() - 60) * 1000}))
        monkeypatch.setenv("CCPROXY_ANTHROPIC_OAUTH_TOKEN_FILE", str(path))
        with pytest.raises(ValueError, match="expired"):
            callback._get_live_oauth_token("anthropic")

    def test_snapshot_does_not_override_separate_router_fallback_account(self, callback, monkeypatch, tmp_path):
        path = tmp_path / "oauth.json"
        path.write_text(json.dumps({"accessToken": "fresh-login", "expiresAt": (time.time() + 3600) * 1000}))
        monkeypatch.setenv("CCPROXY_ANTHROPIC_OAUTH_TOKEN_FILE", str(path))
        assert callback._anthropic_oauth_token_for_model(
            "fallback-claude-fable-5-1", "fallback-claude-fable-5-1", fallback_depth=1
        ) == FALLBACK_TOKEN


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

    def test_fallback_token_429_caps_primary_cooldown(self, callback) -> None:
        class _Response:
            status_code = 429

        callback._maybe_arm_anthropic_cooldown(
            {"model": "claude-opus-4-8", "metadata": {"model_group": "claude-opus-4-8"}},
            _Response(),
        )
        armed = callback._anthropic_primary_cooldown_until
        callback._maybe_arm_anthropic_cooldown(
            {
                "model": "claude-opus-4-8",
                "api_key": FALLBACK_TOKEN,
                "metadata": {"model_group": "claude-opus-4-8"},
            },
            _Response(),
        )
        remaining = callback._anthropic_primary_cooldown_until - time.monotonic()
        assert remaining < 90
        assert callback._anthropic_primary_cooldown_until < armed


class TestRateLimitDetectionPrecision:
    """The cooldown heuristic must not arm on messages that merely contain
    'rate' or digit '429' substrings (request ids, 'generate', 'accurate')."""

    def _armed(self, callback, status_code, message):
        callback._anthropic_primary_cooldown_until = 0.0

        class _Response:
            pass

        response = _Response()
        response.status_code = status_code
        response.message = message
        kwargs = {"model": "claude-opus-4-8", "metadata": {"model_group": "claude-opus-4-8"}}
        callback._maybe_arm_anthropic_cooldown(kwargs, response)
        return callback._anthropic_primary_cooldown_until > 0.0

    def test_status_429_arms(self, callback) -> None:
        assert self._armed(callback, 429, "")

    def test_rate_limit_message_arms(self, callback) -> None:
        assert self._armed(callback, None, "Rate limit exceeded for API")

    def test_too_many_requests_arms(self, callback) -> None:
        assert self._armed(callback, None, "429: Too Many Requests")

    def test_quota_message_arms(self, callback) -> None:
        assert self._armed(callback, None, "usage limit reached: quota exhausted")

    def test_request_id_containing_429_does_not_arm(self, callback) -> None:
        assert not self._armed(callback, 500, "request req_4293abc failed upstream")

    def test_generate_substring_does_not_arm(self, callback) -> None:
        assert not self._armed(callback, 500, "failed to generate completion")

    def test_accurate_substring_does_not_arm(self, callback) -> None:
        assert not self._armed(callback, 400, "model not accurate for request")

    def test_500_without_markers_does_not_arm(self, callback) -> None:
        assert not self._armed(callback, 500, "internal server error")
