#!/usr/bin/env python3
"""Tier-1 Codex parity: terse directive + shape detection for Responses payloads.

Codex talks the OpenAI Responses API (top-level `instructions` string + `input`
list). The Claude-Code-era `_inject_terse_directive` only mutated Anthropic
`system`/`messages`, so a Codex request over a bridged model paid full output
tokens. These tests pin the Responses branch and the shared shape helper.
"""

from __future__ import annotations

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
		"ccproxy_codex_parity_under_test", module_path
	)
	if spec is None or spec.loader is None:
		raise RuntimeError(f"Could not load {module_path}")
	module = importlib.util.module_from_spec(spec)
	sys.modules[spec.name] = module
	spec.loader.exec_module(module)
	return module


_MOD = _load_callback_module()


# ── shape helper ─────────────────────────────────────────────────────
def test_responses_shape_true_for_instructions() -> None:
	assert _MOD._request_is_responses_shape(
		{"instructions": "be helpful", "input": [{"role": "user", "content": "hi"}]}
	)


def test_responses_shape_true_for_input_list_only() -> None:
	# input list without messages is the Responses marker even sans instructions.
	assert _MOD._request_is_responses_shape(
		{"input": [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}]}
	)


def test_responses_shape_false_for_anthropic() -> None:
	assert not _MOD._request_is_responses_shape(
		{"system": "s", "messages": [{"role": "user", "content": "hi"}]}
	)


def test_responses_shape_false_for_chat_completions() -> None:
	assert not _MOD._request_is_responses_shape(
		{"messages": [{"role": "user", "content": "hi"}]}
	)


def test_responses_shape_false_for_junk() -> None:
	assert not _MOD._request_is_responses_shape({})
	assert not _MOD._request_is_responses_shape(None)  # type: ignore[arg-type]


def test_responses_shape_false_for_embeddings_string_list() -> None:
	# {"input": ["doc a", "doc b"]} is embeddings/moderation, NOT Responses.
	# Matching it would make terse inject `instructions` -> OpenAI 400.
	assert not _MOD._request_is_responses_shape({"input": ["doc a", "doc b"]})


def test_responses_shape_true_for_string_input_with_instructions() -> None:
	# Responses also accepts a plain-string `input` alongside `instructions`.
	assert _MOD._request_is_responses_shape({"instructions": "s", "input": "hi"})


def test_responses_shape_false_for_multimodal_embeddings() -> None:
	# Typed embedding items ({"type": "image_url"|"text"}) are NOT Responses items.
	assert not _MOD._request_is_responses_shape(
		{"input": [{"type": "image_url", "image_url": "x"}]}
	)
	assert not _MOD._request_is_responses_shape({"input": [{"type": "text", "text": "x"}]})


def test_responses_shape_true_for_typed_message_items() -> None:
	assert _MOD._request_is_responses_shape(
		{"input": [{"type": "message", "role": "user", "content": "hi"}]}
	)


def test_responses_shape_true_for_codex_custom_tool_items() -> None:
	# Codex apply_patch history: custom_tool_call_output items, no instructions.
	assert _MOD._request_is_responses_shape(
		{"input": [{"type": "custom_tool_call_output", "output": "ok"}]}
	)
	assert _MOD._request_is_responses_shape(
		{"input": [{"type": "local_shell_call", "action": {}}]}
	)


def test_terse_exempts_native_chatgpt() -> None:
	"""Native Codex prompt must not be mutated (cache/behavior sensitive)."""
	os.environ["CCPROXY_TERSE_MODE"] = "on"
	try:
		data = {"model": "chatgpt/gpt-5.6-terra", "instructions": "official", "input": []}
		_MOD._inject_terse_directive(data)
		assert data["instructions"] == "official"
	finally:
		os.environ.pop("CCPROXY_TERSE_MODE", None)


def test_repair_bypass_survives_bridge_via_stamp() -> None:
	"""Stamp set at pre-call keeps the bypass after the bridge adds `messages`."""
	# Post-bridge data looks like Chat Completions but carries the stamp.
	data = {"model": "glm-5.2", "messages": [{"role": "user", "content": "x"}],
	        "metadata": {"ccproxy_responses_shape": True}}
	assert _MOD._response_repair_should_bypass(data)
	# Without the stamp the same post-bridge shape would (correctly) not bypass.
	assert not _MOD._response_repair_should_bypass(
		{"model": "glm-5.2", "messages": [{"role": "user", "content": "x"}]}
	)


def test_terse_noop_on_embeddings_payload() -> None:
	os.environ["CCPROXY_TERSE_MODE"] = "on"
	try:
		data = {"model": "text-embedding-3-small", "input": ["a", "b"]}
		_MOD._inject_terse_directive(data)
		assert "instructions" not in data
	finally:
		os.environ.pop("CCPROXY_TERSE_MODE", None)


# ── terse directive on Responses shape ───────────────────────────────
def test_terse_appends_to_instructions_string() -> None:
	os.environ["CCPROXY_TERSE_MODE"] = "on"
	try:
		data = {"instructions": "You are helpful.", "input": [{"role": "user", "content": "x"}]}
		_MOD._inject_terse_directive(data)
		assert data["instructions"].endswith(_MOD._TERSE_DIRECTIVE)
		assert data["instructions"].startswith("You are helpful.")
	finally:
		os.environ.pop("CCPROXY_TERSE_MODE", None)


def test_terse_instructions_is_byte_stable() -> None:
	"""Idempotent: a second pass must not double-append (cache-prefix safety)."""
	os.environ["CCPROXY_TERSE_MODE"] = "on"
	try:
		data = {"instructions": "sys", "input": []}
		_MOD._inject_terse_directive(data)
		once = data["instructions"]
		_MOD._inject_terse_directive(data)
		assert data["instructions"] == once
	finally:
		os.environ.pop("CCPROXY_TERSE_MODE", None)


def test_terse_creates_instructions_when_absent() -> None:
	"""Responses request with only `input` still gets the budget directive."""
	os.environ["CCPROXY_TERSE_MODE"] = "on"
	try:
		data = {"input": [{"role": "user", "content": "x"}]}
		_MOD._inject_terse_directive(data)
		assert data.get("instructions", "").endswith(_MOD._TERSE_DIRECTIVE)
	finally:
		os.environ.pop("CCPROXY_TERSE_MODE", None)


def test_terse_disabled_is_noop_on_responses() -> None:
	os.environ.pop("CCPROXY_TERSE_MODE", None)
	data = {"instructions": "sys", "input": []}
	_MOD._inject_terse_directive(data)
	assert data["instructions"] == "sys"


# ── Tier 2: response-repair bypass for Responses shape ───────────────
def test_repair_bypass_true_for_native_chatgpt() -> None:
	assert _MOD._response_repair_should_bypass({"model": "chatgpt/gpt-5.6-terra"})
	assert _MOD._response_repair_should_bypass({"model": "gpt-5.6-terra"})
	assert _MOD._response_repair_should_bypass({"model": "gpt-5.6-sol"})
	assert _MOD._response_repair_should_bypass({"model": "gpt-5.6-luna"})
	assert _MOD._response_repair_should_bypass({"model": "gpt-5.5"})
	assert _MOD._is_chatgpt_native_responses_request({"model": "gpt-5.6-terra"})


def test_repair_bypass_true_for_bridged_responses() -> None:
	# glm via /v1/responses bridge: Responses shape, non-chatgpt model.
	assert _MOD._response_repair_should_bypass(
		{"model": "glm-5.2", "instructions": "s", "input": [{"role": "user", "content": "x"}]}
	)


def test_repair_bypass_false_for_anthropic() -> None:
	assert not _MOD._response_repair_should_bypass(
		{"model": "claude-opus-4-8", "system": "s", "messages": [{"role": "user", "content": "hi"}]}
	)


def test_repair_bypass_false_for_chat_completions() -> None:
	assert not _MOD._response_repair_should_bypass(
		{"model": "mistral", "messages": [{"role": "user", "content": "hi"}]}
	)


# ── Tier 3 (A4): compression must skip Responses shape safely ─────────
def test_compression_skips_responses_shape() -> None:
	comp = _load_compression_module()
	inst = comp.ClaudeAwareCompression()
	data = {"model": "glm-5.2", "instructions": "s", "input": [{"role": "user", "content": "x"}]}
	# Must not raise and must not mutate a Responses payload (no `messages`).
	assert inst._compress(data) is None
	assert "messages" not in data


def _install_compression_stubs() -> None:
	litellm = sys.modules.setdefault("litellm", types.ModuleType("litellm"))
	integ = types.ModuleType("litellm.integrations")
	custom_logger = types.ModuleType("litellm.integrations.custom_logger")
	core = types.ModuleType("litellm.litellm_core_utils")
	tokc = types.ModuleType("litellm.litellm_core_utils.token_counter")
	tutils = types.ModuleType("litellm.types.utils")
	ltypes = types.ModuleType("litellm.types")

	class CustomLogger:
		def __init__(self, *_a, **_k) -> None: ...

	def token_counter(*_a, **_k) -> int:
		return 0

	class CallTypes:
		anthropic_messages = "anthropic_messages"
		acompletion = "acompletion"
		completion = "completion"

	custom_logger.CustomLogger = CustomLogger
	tokc.token_counter = token_counter
	tutils.CallTypes = CallTypes
	sys.modules["litellm.integrations"] = integ
	sys.modules["litellm.integrations.custom_logger"] = custom_logger
	sys.modules["litellm.litellm_core_utils"] = core
	sys.modules["litellm.litellm_core_utils.token_counter"] = tokc
	sys.modules["litellm.types"] = ltypes
	sys.modules["litellm.types.utils"] = tutils
	setattr(litellm, "integrations", integ)


def _load_compression_module():
	_install_compression_stubs()
	repo_root = Path(__file__).resolve().parents[1]
	module_path = repo_root / "config" / "claude_aware_compression.py"
	spec = importlib.util.spec_from_file_location(
		"claude_aware_compression_under_test", module_path
	)
	if spec is None or spec.loader is None:
		raise RuntimeError(f"Could not load {module_path}")
	module = importlib.util.module_from_spec(spec)
	sys.modules[spec.name] = module
	spec.loader.exec_module(module)
	return module


def _run() -> None:
	fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
	for fn in fns:
		fn()
		print(f"PASS: {fn.__name__}")
	print(f"\n{len(fns)} passed")


if __name__ == "__main__":
	_run()
