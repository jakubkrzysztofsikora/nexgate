"""provider_specific_header arrives as a dict OR a list of scoped dicts.

LiteLLM 1.100 emits a list whenever a request carries both Anthropic API
headers (anthropic-version/-beta) and an Anthropic OAuth authorization header
(sk-ant-oat...). The OAuth injection path used to index the list like a dict,
which 500'd every Claude Code Opus call routed through the gateway.
"""

from __future__ import annotations

import importlib.util
import types
from pathlib import Path

import pytest

from conftest import install_stub


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "runtime/config/ccproxy_callback.py"

PRIMARY_TOKEN = "sk-ant-primary-test"
CLIENT_TOKEN = "sk-ant-oat01-client-token"


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

        def get_oauth_user_agent(self, _provider: str) -> str | None:
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


@pytest.fixture()
def callback(monkeypatch: pytest.MonkeyPatch):
    module = load_callback_module(monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", PRIMARY_TOKEN)
    monkeypatch.delenv("ANTHROPIC_FALLBACK_TOKEN", raising=False)
    monkeypatch.setattr(module, "_OAUTH_TOKEN_CACHE", {})
    monkeypatch.setattr(module, "_anthropic_primary_cooldown_until", 0.0)
    return module


def _scoped_list() -> list[dict]:
    return [
        {
            "custom_llm_provider": "anthropic,bedrock,vertex_ai",
            "extra_headers": {
                "anthropic-version": "2023-06-01",
                "anthropic-beta": "oauth-2025-04-20",
            },
        },
        {
            "custom_llm_provider": "anthropic",
            "extra_headers": {"authorization": f"Bearer {CLIENT_TOKEN}"},
        },
    ]


def _anthropic_request(scope: object) -> dict:
    return {
        "model": "claude-opus-4-8",
        "messages": [],
        "provider_specific_header": scope,
        "proxy_server_request": {"headers": {"user-agent": "claude-cli/2.0.0"}},
    }


def _anthropic_entries(data: dict) -> list[dict]:
    return [entry for entry in data["provider_specific_header"] if isinstance(entry, dict)]


def test_list_shaped_scope_is_updated_without_crashing(callback) -> None:
    data = _anthropic_request(_scoped_list())

    result = callback.forward_provider_oauth(data, {})

    multi_scope, anthropic_scope = _anthropic_entries(result)
    assert multi_scope["extra_headers"]["anthropic-version"] == "2023-06-01"
    assert "authorization" not in multi_scope["extra_headers"]
    assert anthropic_scope["extra_headers"]["authorization"] == f"Bearer {PRIMARY_TOKEN}"
    assert anthropic_scope["extra_headers"]["user-agent"] == "claude-cli/2.0.0"
    assert CLIENT_TOKEN not in str(result)


def test_create_preserves_non_anthropic_dict_scope(callback) -> None:
    data = _anthropic_request({"custom_llm_provider": "bedrock", "extra_headers": {"x-keep": "1"}})

    result = callback.forward_provider_oauth(data, {})

    scopes = _anthropic_entries(result)
    assert {"custom_llm_provider": "bedrock", "extra_headers": {"x-keep": "1"}} in scopes
    anthropic_scope = [entry for entry in scopes if entry["custom_llm_provider"] == "anthropic"]
    assert anthropic_scope[0]["extra_headers"]["authorization"] == f"Bearer {PRIMARY_TOKEN}"


def test_dict_shaped_scope_is_still_accepted(callback) -> None:
    data = _anthropic_request(
        {
            "custom_llm_provider": "anthropic",
            "extra_headers": {"authorization": f"Bearer {CLIENT_TOKEN}"},
        }
    )

    result = callback.forward_provider_oauth(data, {})

    headers = result["provider_specific_header"]["extra_headers"]
    assert headers["authorization"] == f"Bearer {PRIMARY_TOKEN}"


def test_missing_scope_is_created_for_anthropic(callback) -> None:
    data = _anthropic_request(None)
    data.pop("provider_specific_header")

    result = callback.forward_provider_oauth(data, {})

    created = result["provider_specific_header"]
    assert created["custom_llm_provider"] == "anthropic"
    assert created["extra_headers"]["authorization"] == f"Bearer {PRIMARY_TOKEN}"


def test_strip_client_auth_headers_covers_every_scoped_entry(callback) -> None:
    data = _anthropic_request(_scoped_list())

    callback._strip_client_auth_headers(data)

    for entry in _anthropic_entries(data):
        headers = entry["extra_headers"]
        for banned in ("authorization", "x-api-key", "anthropic-beta", "anthropic-version"):
            assert banned not in headers
