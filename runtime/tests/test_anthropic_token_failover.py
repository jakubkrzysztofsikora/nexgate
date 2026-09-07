import importlib.util
import asyncio
import os
import sys
import time
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _load_module():
	def _stub(name):
		mod = types.ModuleType(name)
		sys.modules[name] = mod
		return mod

	ccproxy = _stub("ccproxy")
	ccproxy_config = _stub("ccproxy.config")
	ccproxy.config = ccproxy_config

	class CCProxyConfig:
		pass

	ccproxy_config.CCProxyConfig = CCProxyConfig
	ccproxy_config.get_config = lambda: types.SimpleNamespace(
		oat_sources={}, get_oauth_token=lambda p: None
	)

	ccproxy_handler = _stub("ccproxy.handler")
	ccproxy.handler = ccproxy_handler

	class CCProxyHandler:
		pass

	ccproxy_handler.CCProxyHandler = CCProxyHandler

	litellm = _stub("litellm")
	core = _stub("litellm.litellm_core_utils")
	prov = _stub("litellm.litellm_core_utils.get_llm_provider_logic")
	litellm.litellm_core_utils = core
	core.get_llm_provider_logic = prov
	prov.get_llm_provider = lambda *a, **k: (None, None, None, None)

	path = _ROOT / "config" / "ccproxy_callback.py"
	spec = importlib.util.spec_from_file_location("ccproxy_callback", path)
	mod = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(mod)
	return mod


cc = _load_module()


def _reset(cooldown_until=0.0):
	cc._anthropic_primary_cooldown_until = cooldown_until
	cc._OAUTH_TOKEN_CACHE.clear()
	os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
	os.environ.pop("ANTHROPIC_FALLBACK_TOKEN", None)


class _Resp:
	def __init__(self, status_code=None, message=""):
		self.status_code = status_code
		self.message = message


def test_1_no_cooldown_primary():
	_reset()
	os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = "primary"
	assert cc._get_live_oauth_token("anthropic") == "primary"


def test_2_cooldown_active_fallback():
	_reset(cooldown_until=time.monotonic() + 100)
	os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = "primary"
	os.environ["ANTHROPIC_FALLBACK_TOKEN"] = "fallback"
	assert cc._get_live_oauth_token("anthropic") == "fallback"
	assert "anthropic" not in cc._OAUTH_TOKEN_CACHE


def test_3_cooldown_active_no_fallback():
	_reset(cooldown_until=time.monotonic() + 100)
	os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = "primary"
	assert cc._get_live_oauth_token("anthropic") == "primary"


def test_4_cooldown_expired_primary():
	_reset(cooldown_until=time.monotonic() - 100)
	os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = "primary"
	os.environ["ANTHROPIC_FALLBACK_TOKEN"] = "fallback"
	assert cc._get_live_oauth_token("anthropic") == "primary"


def test_5_arm_on_429():
	_reset()
	os.environ["ANTHROPIC_FALLBACK_TOKEN"] = "fallback"
	before = time.monotonic()
	cc._maybe_arm_anthropic_cooldown(
		{"model": "claude-opus-4-8"}, _Resp(status_code=429)
	)
	assert cc._anthropic_primary_cooldown_until > before


def test_6_no_arm_on_500():
	_reset()
	os.environ["ANTHROPIC_FALLBACK_TOKEN"] = "fallback"
	cc._maybe_arm_anthropic_cooldown(
		{"model": "claude-opus-4-8"}, _Resp(status_code=500, message="boom")
	)
	assert cc._anthropic_primary_cooldown_until == 0.0


def test_7_failure_callback_arms_cooldown_from_litellm_exception():
	"""LiteLLM passes the exception in kwargs and response_obj as None."""
	_reset()
	os.environ["ANTHROPIC_FALLBACK_TOKEN"] = "fallback"
	before = time.monotonic()
	asyncio.run(
		cc.ccproxy_handler.async_log_failure_event(
			{"model": "claude-opus-4-8", "exception": _Resp(status_code=429)},
			None,
			None,
			None,
		)
	)
	assert cc._anthropic_primary_cooldown_until > before


def test_8_fb_group_model_gets_fb_token():
	_reset()
	os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = "primary"
	os.environ["ANTHROPIC_FALLBACK_TOKEN"] = "fallback"
	assert (
		cc._anthropic_oauth_token_for_model("fallback-claude-opus-5[1m]")
		== "fallback"
	)


def test_9_fb_group_metadata_gets_fb_token():
	"""Provider-level model name + fb model_group (router fallback hop)."""
	_reset()
	os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = "primary"
	os.environ["ANTHROPIC_FALLBACK_TOKEN"] = "fallback"
	assert (
		cc._anthropic_oauth_token_for_model(
			"claude-opus-4-8", "fallback-claude-opus-4-8[1m]"
		)
		== "fallback"
	)


def test_10_fb_group_no_fb_env_degrades_to_live():
	_reset()
	os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = "primary"
	assert cc._anthropic_oauth_token_for_model("fallback-claude-opus-5") == "primary"


def test_11_primary_group_gets_live_token():
	_reset()
	os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = "primary"
	os.environ["ANTHROPIC_FALLBACK_TOKEN"] = "fallback"
	assert cc._anthropic_oauth_token_for_model("claude-opus-5[1m]") == "primary"


def test_12_fb_group_wins_over_active_cooldown_reverse():
	"""Cooldown returns fb token for primary groups; fb group without cooldown
	still returns fb token — both paths converge on the same account."""
	_reset()
	os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = "primary"
	os.environ["ANTHROPIC_FALLBACK_TOKEN"] = "fallback"
	assert cc._anthropic_oauth_token_for_model("fallback-claude-haiku-4-5-20251001") == "fallback"
	assert cc._get_live_oauth_token("anthropic") == "primary"


def test_13_no_arm_on_fb_model_group():
	_reset(cooldown_until=0.0)
	os.environ["ANTHROPIC_FALLBACK_TOKEN"] = "fallback"
	cc._maybe_arm_anthropic_cooldown(
		{
			"model": "claude-opus-4-8",
			"metadata": {"model_group": "fallback-claude-opus-4-8[1m]"},
		},
		_Resp(status_code=429),
	)
	assert cc._anthropic_primary_cooldown_until == 0.0


def test_14_arm_on_primary_group_reached_as_chain_fallback():
	"""claude-opus-4-8[1m] sits in chatgpt/* chains: that hop uses the primary
	token, so its 429 must still arm the cooldown even at fallback_depth>0."""
	_reset(cooldown_until=0.0)
	os.environ["ANTHROPIC_FALLBACK_TOKEN"] = "fallback"
	before = time.monotonic()
	cc._maybe_arm_anthropic_cooldown(
		{
			"model": "claude-opus-4-8",
			"metadata": {"model_group": "claude-opus-4-8[1m]"},
			"fallback_depth": 1,
		},
		_Resp(status_code=429),
	)
	assert cc._anthropic_primary_cooldown_until > before


def test_15_no_rearm_while_cooldown_active():
	_reset(cooldown_until=time.monotonic() + 100)
	os.environ["ANTHROPIC_FALLBACK_TOKEN"] = "fallback"
	deadline = cc._anthropic_primary_cooldown_until
	cc._maybe_arm_anthropic_cooldown(
		{"model": "claude-opus-4-8"}, _Resp(status_code=429)
	)
	assert cc._anthropic_primary_cooldown_until == deadline


def test_16_rearm_after_cooldown_expiry():
	_reset(cooldown_until=time.monotonic() - 1)
	os.environ["ANTHROPIC_FALLBACK_TOKEN"] = "fallback"
	before = time.monotonic()
	cc._maybe_arm_anthropic_cooldown(
		{"model": "claude-opus-4-8"}, _Resp(status_code=429)
	)
	assert cc._anthropic_primary_cooldown_until > before


if __name__ == "__main__":
	for name, fn in sorted(globals().items()):
		if name.startswith("test_") and callable(fn):
			fn()
			print(name, "ok")
	print("all passed")
