#!/usr/bin/env python3
"""Tests for Anthropic tool-search (deferred loading) injection."""

from __future__ import annotations

import copy
import importlib.util
import os
import sys
import types
from pathlib import Path


def _install_import_stubs() -> None:
	ccproxy = types.ModuleType("ccproxy")
	config = types.ModuleType("ccproxy.config")
	handler = types.ModuleType("ccproxy.handler")
	provider_logic = types.ModuleType(
		"litellm.litellm_core_utils.get_llm_provider_logic"
	)

	class DummyConfig:
		debug = False
		oat_sources: dict[str, str] = {}

		def load_hooks(self) -> list:
			return []

		def get_oauth_token(self, _provider_name: str) -> None:
			return None

		def get_oauth_user_agent(self, _provider_name: str) -> None:
			return None

	def get_config() -> DummyConfig:
		return DummyConfig()

	class CCProxyHandler:
		def __init__(self, *_args, **_kwargs) -> None:
			self.hooks: list = []

	def get_llm_provider(**_kwargs):
		return "", "", None, None

	config.get_config = get_config
	handler.CCProxyHandler = CCProxyHandler
	provider_logic.get_llm_provider = get_llm_provider

	sys.modules.setdefault("ccproxy", ccproxy)
	sys.modules["ccproxy.config"] = config
	sys.modules["ccproxy.handler"] = handler
	sys.modules["litellm.litellm_core_utils.get_llm_provider_logic"] = provider_logic


def _load_callback_module():
	_install_import_stubs()
	repo_root = Path(__file__).resolve().parents[1]
	module_path = repo_root / "config" / "ccproxy_callback.py"
	spec = importlib.util.spec_from_file_location(
		"ccproxy_tool_search_under_test", module_path
	)
	if spec is None or spec.loader is None:
		raise RuntimeError(f"Could not load {module_path}")
	module = importlib.util.module_from_spec(spec)
	sys.modules[spec.name] = module
	spec.loader.exec_module(module)
	return module


def _clear_env() -> None:
	for name in ("CCPROXY_TOOL_SEARCH", "CCPROXY_TOOL_SEARCH_MIN"):
		os.environ.pop(name, None)


def _make_tools(n: int) -> list[dict]:
	"""Create n dummy tools with unique names."""
	return [
		{
			"name": f"tool_{i}",
			"description": f"Tool number {i}",
			"input_schema": {"type": "object", "properties": {}},
		}
		for i in range(n)
	]


def test_tool_search_disabled_by_default() -> None:
	"""Without CCPROXY_TOOL_SEARCH=on, the injector is a no-op."""
	module = _load_callback_module()
	_clear_env()
	data = {
		"model": "claude-opus-4-8",
		"messages": [{"role": "user", "content": "test"}],
		"tools": _make_tools(50),
	}
	module._apply_tool_search_defer(data)
	assert not any(t.get("defer_loading") for t in data["tools"])
	assert not any(
		t.get("type", "").startswith("tool_search_tool") for t in data["tools"]
	)


def test_tool_search_enabled_injects_deferral() -> None:
	"""With CCPROXY_TOOL_SEARCH=on, non-hot tools are deferred."""
	_clear_env()
	os.environ["CCPROXY_TOOL_SEARCH"] = "on"
	module = _load_callback_module()
	hot_names = sorted(module._TOOL_SEARCH_HOT_TOOLS)
	total = len(hot_names) + 20  # 20 cold tools
	tools = _make_tools(total)
	# Name the first N tools to match the hot-set so they stay non-deferred
	for i, name in enumerate(hot_names):
		tools[i]["name"] = name
	data = {
		"model": "claude-opus-4-8",
		"anthropic_version": "2023-06-01",
		"messages": [{"role": "user", "content": "test"}],
		"tools": tools,
	}
	module._apply_tool_search_defer(data)
	deferred = [t for t in data["tools"] if t.get("defer_loading")]
	non_deferred = [t for t in data["tools"] if not t.get("defer_loading")]
	# Non-hot tools should be deferred; hot tools + search tool are not
	assert len(deferred) == total - len(hot_names)
	assert len(non_deferred) == len(hot_names) + 1  # hot + search
	search_tool = data["tools"][-1]
	assert search_tool["type"] == "tool_search_tool_bm25_20251119"


def test_tool_search_below_min_threshold() -> None:
	"""Requests with fewer than TOOL_SEARCH_MIN tools pass through unmodified."""
	_clear_env()
	os.environ["CCPROXY_TOOL_SEARCH"] = "on"
	os.environ["CCPROXY_TOOL_SEARCH_MIN"] = "30"
	module = _load_callback_module()
	data = {
		"model": "claude-opus-4-8",
		"anthropic_version": "2023-06-01",
		"messages": [{"role": "user", "content": "test"}],
		"tools": _make_tools(10),
	}
	module._apply_tool_search_defer(data)
	assert not any(t.get("defer_loading") for t in data["tools"])
	assert not any(
		t.get("type", "").startswith("tool_search_tool") for t in data["tools"]
	)


def test_tool_search_no_double_injection() -> None:
	"""A request that already has a tool_search_tool entry is not double-injected."""
	_clear_env()
	os.environ["CCPROXY_TOOL_SEARCH"] = "on"
	module = _load_callback_module()
	tools = _make_tools(50)
	tools.append({"type": "tool_search_tool_bm25_20251119", "name": "tool_search_tool_bm25"})
	data = {
		"model": "claude-opus-4-8",
		"anthropic_version": "2023-06-01",
		"messages": [{"role": "user", "content": "test"}],
		"tools": tools,
	}
	module._apply_tool_search_defer(data)
	search_tools = [
		t for t in data["tools"] if t.get("type", "").startswith("tool_search_tool")
	]
	assert len(search_tools) == 1


def test_tool_search_non_claude_model_bypass() -> None:
	"""Non-Claude models skip the injector entirely."""
	_clear_env()
	os.environ["CCPROXY_TOOL_SEARCH"] = "on"
	module = _load_callback_module()
	data = {
		"model": "deepseek-chat",
		"anthropic_version": "2023-06-01",
		"messages": [{"role": "user", "content": "test"}],
		"tools": _make_tools(50),
	}
	module._apply_tool_search_defer(data)
	assert not any(t.get("defer_loading") for t in data["tools"])
	assert not any(
		t.get("type", "").startswith("tool_search_tool") for t in data["tools"]
	)


def test_tool_search_strips_cache_control() -> None:
	"""Deferred tools must NOT carry cache_control (defer+cache_control = 400)."""
	_clear_env()
	os.environ["CCPROXY_TOOL_SEARCH"] = "on"
	module = _load_callback_module()
	tools = [
		{"name": "cold_tool", "input_schema": {}, "cache_control": {"type": "ephemeral"}}
	] * 40
	tools[0]["name"] = "Read"  # a hot tool keeps its cache_control
	data = {
		"model": "claude-opus-4-8",
		"anthropic_version": "2023-06-01",
		"messages": [{"role": "user", "content": "test"}],
		"tools": tools,
	}
	module._apply_tool_search_defer(data)
	for t in data["tools"]:
		if t.get("defer_loading"):
			assert "cache_control" not in t
	# The hot tool ("Read") is non-deferred and must RETAIN its cache_control.
	read_tool = next(t for t in data["tools"] if t.get("name") == "Read")
	assert "defer_loading" not in read_tool
	assert read_tool.get("cache_control") == {"type": "ephemeral"}


def test_strip_tool_search_artifacts_on_fallback() -> None:
	"""Non-Claude fallback strips tool_search_tool_* and defer_loading from tools[]."""
	module = _load_callback_module()
	_clear_env()
	tools = [
		{"name": "hot", "input_schema": {}},
		{"name": "deferred", "defer_loading": True},
		{"type": "tool_search_tool_bm25_20251119", "name": "search"},
	]
	kwargs = {"tools": copy.deepcopy(tools)}
	module._strip_tool_search_artifacts(kwargs)
	result = kwargs["tools"]
	assert len(result) == 2  # hot + deferred (search stripped)
	assert result[0]["name"] == "hot"
	assert result[1]["name"] == "deferred"
	assert "defer_loading" not in result[1]


def test_sanitize_content_blocks_strips_server_tool_use() -> None:
	"""server_tool_use blocks are flattened to text for non-Anthropic providers."""
	module = _load_callback_module()
	blocks = [
		{"type": "text", "text": "hello"},
		{"type": "server_tool_use", "name": "get_weather"},
	]
	sanitized = module._sanitize_content_blocks(blocks, "deepseek")
	assert len(sanitized) == 2
	assert sanitized[1]["type"] == "text"
	assert "[tool search: discovered get_weather]" in sanitized[1]["text"]


def test_sanitize_content_blocks_strips_tool_reference() -> None:
	"""tool_reference blocks are flattened to text for non-Anthropic providers."""
	module = _load_callback_module()
	blocks = [
		{"type": "text", "text": "hello"},
		{"type": "tool_reference", "id": "ref-123"},
	]
	sanitized = module._sanitize_content_blocks(blocks, "glm-4.5")
	assert len(sanitized) == 2
	assert sanitized[1]["type"] == "text"
	assert "[tool search:" in sanitized[1]["text"]


def test_sanitize_content_blocks_preserves_anthropic_blocks() -> None:
	"""Anthropic requests preserve server_tool_use/tool_reference blocks byte-identical."""
	module = _load_callback_module()
	blocks = [
		{"type": "text", "text": "hello"},
		{"type": "server_tool_use", "name": "get_weather"},
		{"type": "tool_reference", "id": "ref-123"},
	]
	sanitized = module._sanitize_content_blocks(blocks, "anthropic")
	assert len(sanitized) == 3
	assert sanitized[1]["type"] == "server_tool_use"
	assert sanitized[2]["type"] == "tool_reference"


def test_tool_search_merges_beta_header() -> None:
	"""Beta header always gains tool-search beta + context-1m, never duplicate."""
	_clear_env()
	os.environ["CCPROXY_TOOL_SEARCH"] = "on"
	module = _load_callback_module()
	data = {
		"model": "claude-opus-4-8",
		"anthropic_version": "2023-06-01",
		"messages": [{"role": "user", "content": "test"}],
		"tools": _make_tools(40),
		"extra_headers": {"anthropic-beta": "context-1m-2025-08-07"},
	}
	module._apply_tool_search_defer(data)
	beta = data["extra_headers"]["anthropic-beta"]
	parts = set(beta.split(","))
	assert module._TOOL_SEARCH_BETA in parts
	assert module._CLAUDE_ONE_MILLION_CONTEXT_BETA in parts
	# No duplicate context-1m
	assert beta.count(module._CLAUDE_ONE_MILLION_CONTEXT_BETA) == 1


def test_model_supports_tool_search_gate() -> None:
	"""Supported models pass the tool search model gate."""
	module = _load_callback_module()
	assert module._model_supports_tool_search("claude-opus-4-8") is True
	assert module._model_supports_tool_search("claude-opus-4-8[1m]") is True
	assert module._model_supports_tool_search("claude-sonnet-5") is True
	assert module._model_supports_tool_search("claude-fable-5-1") is True
	assert module._model_supports_tool_search("qwencloud/qwen3.7-plus") is False
	assert module._model_supports_tool_search("unsupported-model") is False
	assert module._model_supports_tool_search(None) is False


def test_scan_tool_search_discovery_passthrough_shape() -> None:
	"""Passthrough content_block_start shape yields discovered names + tokens."""
	module = _load_callback_module()
	names, tokens = module._scan_tool_search_discovery({
		"type": "content_block_start",
		"content_block": {"type": "server_tool_use", "name": "get_weather"},
	})
	assert names == ["get_weather"]
	assert tokens == 0
	names, tokens = module._scan_tool_search_discovery({
		"type": "tool_search_tool_result",
		"content": [{"name": "get_weather"}],
	})
	assert "get_weather" in names
	assert tokens > 0


def test_scan_tool_search_discovery_nested_shape() -> None:
	"""Non-passthrough ModelResponse shape (nested message.content) is found."""
	module = _load_callback_module()
	names, _tokens = module._scan_tool_search_discovery({
		"type": "message",
		"message": {
			"content": [
				{"type": "server_tool_use", "name": "read_file"},
				{"type": "tool_reference", "name": "write_file"},
			],
		},
	})
	assert "read_file" in names
	assert "write_file" in names


def test_sanitize_content_blocks_strips_tool_search_tool_result() -> None:
	"""tool_search_tool_result (every-discovery artifact) flattens with names."""
	module = _load_callback_module()
	blocks = [
		{"type": "text", "text": "hello"},
		{
			"type": "tool_search_tool_result",
			"content": [{"name": "get_weather"}, {"name": "forecast_today"}],
		},
	]
	sanitized = module._sanitize_content_blocks(blocks, "deepseek")
	assert len(sanitized) == 2
	assert sanitized[1]["type"] == "text"
	assert "get_weather" in sanitized[1]["text"]
	assert "forecast_today" in sanitized[1]["text"]


def test_search_tools_in_hot_tools() -> None:
	"""Search tools must be hot so they are never deferred."""
	module = _load_callback_module()
	assert "Search" in module._TOOL_SEARCH_HOT_TOOLS
	assert "WebSearch" in module._TOOL_SEARCH_HOT_TOOLS
	assert "Web Search" in module._TOOL_SEARCH_HOT_TOOLS
	assert "ReadURL" in module._TOOL_SEARCH_HOT_TOOLS
	assert "Fetch" in module._TOOL_SEARCH_HOT_TOOLS
