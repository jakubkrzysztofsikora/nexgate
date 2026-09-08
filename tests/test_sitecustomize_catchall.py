"""Catchall fallback rewrite must be opt-in, never silent.

Unknown model names must reach LiteLLM's normal 404 path unless the operator
explicitly enabled legacy catchall rewriting with CCPROXY_CATCHALL_FALLBACK.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

from conftest import _Placeholder, install_stub


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "runtime/config/sitecustomize.py"


class _CCProxyConfigMeta(type):
    def __getattr__(cls, name: str) -> object:
        if name.startswith("__"):
            raise AttributeError(name)
        return _Placeholder()


@pytest.fixture()
def patched_route(monkeypatch: pytest.MonkeyPatch):
    calls: list[dict] = []

    async def recorder(data, llm_router=None, user_model=None, route_type=None, **kwargs):
        calls.append({"data": data, "user_model": user_model, "route_type": route_type})
        return data

    litellm_stub = install_stub(monkeypatch, "litellm", model_cost={})
    utils_stub = install_stub(monkeypatch, "litellm.utils")
    litellm_stub.utils = utils_stub
    ccproxy_cfg = install_stub(
        monkeypatch,
        "ccproxy.config",
        CCProxyConfig=type("CCProxyConfig", (_CCProxyConfigMeta("CCProxyConfigMeta", (object,), {}),), {}),
    )
    install_stub(monkeypatch, "ccproxy").config = ccproxy_cfg
    proxy_stub = install_stub(monkeypatch, "litellm.proxy")
    route_mod = install_stub(monkeypatch, "litellm.proxy.route_llm_request", route_request=recorder)
    common_mod = install_stub(monkeypatch, "litellm.proxy.common_request_processing", route_request=recorder)
    install_stub(monkeypatch, "litellm.proxy.proxy_server", app=object())
    proxy_stub.route_llm_request = route_mod
    proxy_stub.common_request_processing = common_mod

    spec = importlib.util.spec_from_file_location("sitecustomize_under_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    installed = sys.modules["litellm.proxy.route_llm_request"].route_request
    assert installed is not recorder, "sitecustomize did not install its route patch on the stub"

    class _Router:
        model_names = ["known-model"]
        model_group_alias = {}
        deployment_names = []
        default_fallbacks = ["minimax-m3"]
        fallbacks = {}

    return monkeypatch, installed, _Router(), calls


def _run(installed, data, router):
    return asyncio.run(installed(data, router, "unknown-model-x", "anthropic_messages"))


class TestCatchallFallbackIsOptIn:
    def test_unknown_model_is_not_rewritten_by_default(self, patched_route) -> None:
        monkeypatch, installed, router, _calls = patched_route
        monkeypatch.delenv("CCPROXY_CATCHALL_FALLBACK", raising=False)
        data = {"model": "totally-unknown-model", "metadata": {}}
        _run(installed, data, router)
        assert data["model"] == "totally-unknown-model"
        assert "original_requested_model" not in data["metadata"]

    def test_unknown_model_is_rewritten_when_opted_in(self, patched_route) -> None:
        monkeypatch, installed, router, _calls = patched_route
        monkeypatch.setenv("CCPROXY_CATCHALL_FALLBACK", "1")
        data = {"model": "totally-unknown-model", "metadata": {}}
        _run(installed, data, router)
        assert data["model"] == "minimax-m3"
        assert data["metadata"]["original_requested_model"] == "totally-unknown-model"

    def test_known_model_is_never_rewritten(self, patched_route) -> None:
        monkeypatch, installed, router, _calls = patched_route
        monkeypatch.setenv("CCPROXY_CATCHALL_FALLBACK", "1")
        data = {"model": "known-model", "metadata": {}}
        _run(installed, data, router)
        assert data["model"] == "known-model"

    def test_opt_in_flag_off_values_do_not_enable(self, patched_route) -> None:
        monkeypatch, installed, router, _calls = patched_route
        for value in ("0", "false", "no", "off", ""):
            monkeypatch.setenv("CCPROXY_CATCHALL_FALLBACK", value)
            data = {"model": "totally-unknown-model", "metadata": {}}
            _run(installed, data, router)
            assert data["model"] == "totally-unknown-model", value
