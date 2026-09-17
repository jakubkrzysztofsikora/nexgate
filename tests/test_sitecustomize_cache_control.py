"""Native /v1/messages must not forward cache_control_injection_points upstream.

LiteLLM 1.100's cache-control hook writes back the points it cannot apply
(tool_config) into kwargs; on the Anthropic-native path nothing consumes them,
so they ride into the request body and Anthropic 400s with
"cache_control_injection_points: Extra inputs are not permitted". The
sitecustomize patch drops the leftover on the native path only.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

import pytest

from conftest import install_stub


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "runtime/config/sitecustomize.py"


class _FakeCacheControlHook:
    """Mimics 1.100 write-back of unapplied tool_config injection points."""

    @staticmethod
    def maybe_inject_cache_control(
        messages, system, kwargs, model=None, custom_llm_provider=None, tools=None, api_base=None
    ):
        if kwargs.get("cache_control_injection_points"):
            kwargs["cache_control_injection_points"] = [
                {"location": "tool_config", "_litellm_judged": True}
            ]
        return messages, system

    def get_chat_completion_prompt(self, model, messages, non_default_params, **kwargs):
        if non_default_params.get("cache_control_injection_points"):
            non_default_params["cache_control_injection_points"] = [
                {"location": "tool_config", "_litellm_judged": True}
            ]
        return model, messages, non_default_params

    async def async_get_chat_completion_prompt(self, model, messages, non_default_params, **kwargs):
        return self.get_chat_completion_prompt(model, messages, non_default_params, **kwargs)


class _FakeAdapter:
    def translate_anthropic_tools_to_openai(self, tools, model=None):
        return tools, {}

    def translate_anthropic_tool_choice_to_openai(self, tool_choice):
        return tool_choice

    def translate_anthropic_to_openai(self, anthropic_message_request, *, custom_llm_provider=None):
        return {"model": "stub"}, {}

    def _translate_openai_content_to_anthropic(self, choices, tool_name_mapping=None):
        return []

    def translate_openai_response_to_anthropic(self, response, tool_name_mapping=None, polyfill_result=None):
        return {"content": []}


class _FakeHandler:
    async def async_anthropic_messages_handler(self, *args, **kwargs):
        return None

    def anthropic_messages_handler(self, *args, **kwargs):
        return None

    def _prepare_completion_kwargs(self, *args, **kwargs):
        return {}, {}


@pytest.fixture()
def patched_sitecustomize(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    litellm_stub = install_stub(monkeypatch, "litellm", model_cost={})
    utils_stub = install_stub(monkeypatch, "litellm.utils")
    litellm_stub.utils = utils_stub
    install_stub(monkeypatch, "litellm.main")
    install_stub(monkeypatch, "litellm.litellm_core_utils")
    install_stub(
        monkeypatch,
        "litellm.litellm_core_utils.get_llm_provider_logic",
        get_llm_provider=lambda *a, **k: (None, None, None, None),
    )
    install_stub(monkeypatch, "litellm.litellm_core_utils.token_counter", get_modified_max_tokens=lambda *a, **k: None)
    install_stub(monkeypatch, "litellm.llms")
    install_stub(monkeypatch, "litellm.llms.anthropic")
    install_stub(
        monkeypatch,
        "litellm.llms.anthropic.common_utils",
        ANTHROPIC_OAUTH_TOKEN_PREFIX="sk-ant-oat",
        optionally_handle_anthropic_oauth=lambda headers, api_key: (headers, api_key),
    )
    install_stub(monkeypatch, "litellm.llms.anthropic.experimental_pass_through")
    install_stub(monkeypatch, "litellm.llms.anthropic.experimental_pass_through.messages")
    install_stub(monkeypatch, "litellm.llms.anthropic.experimental_pass_through.messages.transformation")
    install_stub(monkeypatch, "litellm.llms.anthropic.experimental_pass_through.adapters")
    install_stub(
        monkeypatch,
        "litellm.llms.anthropic.experimental_pass_through.adapters.transformation",
        LiteLLMAnthropicMessagesAdapter=_FakeAdapter,
    )
    install_stub(
        monkeypatch,
        "litellm.llms.anthropic.experimental_pass_through.adapters.handler",
        LiteLLMMessagesToCompletionTransformationHandler=_FakeHandler,
    )
    install_stub(monkeypatch, "litellm.integrations")
    install_stub(
        monkeypatch,
        "litellm.integrations.anthropic_cache_control_hook",
        AnthropicCacheControlHook=_FakeCacheControlHook,
    )
    proxy_stub = install_stub(monkeypatch, "litellm.proxy")
    install_stub(monkeypatch, "litellm.proxy.anthropic_endpoints")
    install_stub(monkeypatch, "litellm.proxy.anthropic_endpoints.endpoints")
    install_stub(monkeypatch, "litellm.proxy.proxy_server", app=object())
    proxy_stub.anthropic_endpoints = sys.modules["litellm.proxy.anthropic_endpoints"]

    install_stub(monkeypatch, "ccproxy")
    install_stub(
        monkeypatch,
        "ccproxy.config",
        CCProxyConfig=type("CCProxyConfig", (), {}),
        get_config=lambda: type("Config", (), {"oat_sources": {}})(),
    )

    spec = importlib.util.spec_from_file_location("sitecustomize_cache_control_under_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _call_hook(kwargs: dict, provider: str | None) -> None:
    hook = sys.modules["litellm.integrations.anthropic_cache_control_hook"].AnthropicCacheControlHook
    hook.maybe_inject_cache_control(
        [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        [{"type": "text", "text": "sys"}],
        kwargs,
        model="claude-opus-4-8",
        custom_llm_provider=provider,
        tools=[{"name": "Bash", "description": "d", "input_schema": {"type": "object", "properties": {}}}],
    )


def test_leftover_injection_points_are_dropped_for_anthropic(patched_sitecustomize) -> None:
    for provider in ("anthropic", None):
        kwargs = {"cache_control_injection_points": [{"location": "tool_config"}]}

        _call_hook(kwargs, provider)

        assert "cache_control_injection_points" not in kwargs, provider


def test_leftover_injection_points_survive_for_other_providers(patched_sitecustomize) -> None:
    kwargs = {"cache_control_injection_points": [{"location": "tool_config"}]}

    _call_hook(kwargs, "bedrock")

    assert kwargs["cache_control_injection_points"] == [{"location": "tool_config", "_litellm_judged": True}]


def _hook_class() -> type:
    return sys.modules["litellm.integrations.anthropic_cache_control_hook"].AnthropicCacheControlHook


def test_chat_path_leftovers_dropped_for_anthropic(patched_sitecustomize) -> None:
    hook = _hook_class()()
    for provider in ("anthropic", None):
        non_default_params = {
            "custom_llm_provider": provider,
            "cache_control_injection_points": [{"location": "tool_config"}],
        }

        _, _, result = hook.get_chat_completion_prompt("claude-opus-4-8", [], non_default_params)

        assert "cache_control_injection_points" not in result, provider


def test_chat_path_leftovers_survive_for_bedrock(patched_sitecustomize) -> None:
    hook = _hook_class()()
    non_default_params = {
        "custom_llm_provider": "bedrock",
        "cache_control_injection_points": [{"location": "tool_config"}],
    }

    _, _, result = hook.get_chat_completion_prompt("bedrock-claude", [], non_default_params)

    assert result["cache_control_injection_points"] == [{"location": "tool_config", "_litellm_judged": True}]


def test_async_chat_path_leftovers_dropped_for_anthropic(patched_sitecustomize) -> None:
    hook = _hook_class()()
    non_default_params = {
        "custom_llm_provider": "anthropic",
        "cache_control_injection_points": [{"location": "tool_config"}],
    }

    _, _, result = asyncio.run(
        hook.async_get_chat_completion_prompt("claude-opus-4-8", [], non_default_params)
    )

    assert "cache_control_injection_points" not in result
