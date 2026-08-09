import json
import logging
import os
import litellm

# Configure a basic logger for our startup patches
logger = logging.getLogger("ccproxy.startup_patches")
logger.info("Initializing ccproxy sitecustomize startup patches...")

# 0. Patch LiteLLM 1.95.0 KeyError: 'key' bug in _get_builtin_model_info_for_registration.
#    LiteLLM 1.95.0 introduced a check `info["key"] in litellm.model_cost` in
#    _get_builtin_model_info_for_registration. When a deployment's model_info
#    dict in litellm.yaml lacks a "key" entry (such as deepseek-v4-pro[1m]),
#    info["key"] raises KeyError: 'key', causing Router._create_deployment to
#    fail silently and drop the deployment. Safely check for "key" before access.
try:
	import litellm.utils

	def _patched_get_builtin_model_info_for_registration(model: str):
		try:
			info = litellm.utils.get_model_info(model=model)
		except Exception:
			return None
		if not isinstance(info, dict):
			return None
		key = info.get("key")
		if key and key in litellm.model_cost:
			return info
		if key and litellm.utils.match_capability_generalizations(key) is None:
			return info
		return None

	litellm.utils._get_builtin_model_info_for_registration = (
		_patched_get_builtin_model_info_for_registration
	)
	logger.info("Successfully patched litellm.utils._get_builtin_model_info_for_registration against KeyError: 'key'.")
except Exception as e:
	logger.error(f"Failed to patch _get_builtin_model_info_for_registration: {e}", exc_info=True)

# 0b. Patch CCProxyConfig._load_credentials to be resilient to missing credentials files
try:
	from ccproxy.config import CCProxyConfig
	_original_load_credentials = CCProxyConfig._load_credentials

	def _patched_load_credentials(self):
		try:
			_original_load_credentials(self)
		except Exception as exc:
			logger.warning(
				f"[CCPROXY_CREDENTIALS_PATCH] Failed to load credentials: {exc}. "
				f"Swallowing error to prevent gateway startup failure."
			)
			if not hasattr(self, "_oat_values") or self._oat_values is None:
				self._oat_values = {}
			if not hasattr(self, "_oat_user_agents") or self._oat_user_agents is None:
				self._oat_user_agents = {}
			for provider in getattr(self, "oat_sources", {}):
				if provider not in self._oat_values:
					self._oat_values[provider] = ""

	CCProxyConfig._load_credentials = _patched_load_credentials
	logger.info("Successfully patched CCProxyConfig._load_credentials to be resilient to missing credentials files.")
except Exception as e:
	logger.error(f"Failed to patch CCProxyConfig._load_credentials: {e}", exc_info=True)



# Kill-switch for the custom pre-call context-window enforcement below.
# Disabled: trust each backend (Codex / z.ai / scaleway) to enforce its own
# real context limit. The custom check caused false positives on models whose
# declared window was smaller than the request (e.g. scaleway/glm-5.2).
_ENFORCE_CUSTOM_CONTEXT_CHECK = False

# ChatGPT/Codex routing is intentionally opt-out so the existing deployment
# keeps working, but it can be disabled for a clean provider-baseline run:
#   CCPROXY_DISABLE_CHATGPT_PATCHES=1
# Keep the model-info None guard below enabled even when these provider-specific
# patches are disabled; LiteLLM 1.91.x otherwise constructs ModelInfo(**None)
# for custom providers.
_CHATGPT_PATCHES_ENABLED = os.getenv("CCPROXY_DISABLE_CHATGPT_PATCHES", "").lower() not in {
	"1",
	"true",
	"yes",
	"on",
}
try:
	_CHATGPT_SAFE_MAX_OUTPUT_TOKENS = max(
		4096, int(os.getenv("CCPROXY_CHATGPT_MAX_OUTPUT_TOKENS", "24000"))
	)
except ValueError:
	_CHATGPT_SAFE_MAX_OUTPUT_TOKENS = 24000

def _is_chatgpt_model(model, custom_llm_provider=None, metadata=None) -> bool:
	provider = str(custom_llm_provider or "").lower()
	model_name = str(model or "").lower()
	if provider == "chatgpt" or model_name.startswith("chatgpt/"):
		return True
	if isinstance(metadata, dict):
		for key in ("ccproxy_provider_model", "ccproxy_litellm_model"):
			if str(metadata.get(key) or "").lower().startswith("chatgpt/"):
				return True
	return False

# 1. Register non-Anthropic models dynamically to declare reasoning and tool calling capabilities.
# Without explicit capability entries, drop_params can strip tools/tool_choice
# before the request reaches the provider, breaking agentic workflows.
litellm.model_cost["moonshot/kimi-for-coding"] = {
	"supports_reasoning": True,
	"litellm_provider": "moonshot",
	"supports_function_calling": True,
	"supports_tool_choice": True,
	"supports_parallel_function_calling": True,
	"max_input_tokens": 262144,
	"max_output_tokens": 262144,
	"max_tokens": 262144,
}
logger.info("Successfully registered moonshot/kimi-for-coding tool and reasoning capabilities.")

litellm.model_cost["deepseek/deepseek-v4-pro"] = {
	"mode": "chat",
	"litellm_provider": "deepseek",
	"supports_function_calling": True,
	"supports_tool_choice": True,
	"supports_parallel_function_calling": True,
	"max_input_tokens": 1000000,
	"max_output_tokens": 8192,
	"max_tokens": 1008192,
}
logger.info("Successfully registered deepseek/deepseek-v4-pro tool capability.")

litellm.model_cost["mistral/mistral-vibe-cli-latest"] = {
	"mode": "chat",
	"litellm_provider": "mistral",
	"supports_function_calling": True,
	"supports_tool_choice": True,
	"supports_parallel_function_calling": True,
	"max_input_tokens": 262144,
	"max_output_tokens": 16384,
	"max_tokens": 278528,
}
litellm.model_cost["mistral"] = litellm.model_cost["mistral/mistral-vibe-cli-latest"]
logger.info("Successfully registered mistral/mistral-vibe-cli-latest tool capability.")

litellm.model_cost["zai/glm-5.1"] = {
	"mode": "chat",
	"litellm_provider": "openai",
	"supports_function_calling": True,
	"supports_tool_choice": True,
	"supports_parallel_function_calling": True,
	"max_input_tokens": 200000,
	"max_output_tokens": 16384,
	"max_tokens": 216384,
}
logger.info("Successfully registered zai/glm-5.1 tool capability.")

litellm.model_cost["zai/glm-5.2"] = {
	"mode": "chat",
	"litellm_provider": "openai",
	"supports_function_calling": True,
	"supports_tool_choice": True,
	"supports_parallel_function_calling": True,
	"max_input_tokens": 1000000,
	"max_output_tokens": 16384,
	"max_tokens": 1016384,
}
logger.info("Successfully registered zai/glm-5.2 tool capability.")

litellm.model_cost["minimax/minimax-m3"] = {
	"mode": "chat",
	"litellm_provider": "minimax",
	"supports_function_calling": True,
	"supports_tool_choice": True,
	"supports_parallel_function_calling": True,
	"max_input_tokens": 1048576,
	"max_output_tokens": 32000,
	"max_tokens": 1080576,
}
litellm.model_cost["minimax-m3"] = litellm.model_cost["minimax/minimax-m3"]
litellm.model_cost["chinol"] = litellm.model_cost["minimax/minimax-m3"]
litellm.model_cost["MiniMax-M3"] = litellm.model_cost["minimax/minimax-m3"]
litellm.model_cost["minimax/MiniMax-M3"] = litellm.model_cost["minimax/minimax-m3"]
logger.info("Successfully registered minimax/minimax-m3 tool capability.")

litellm.model_cost["zai/glm-5"] = {
	"mode": "chat",
	"litellm_provider": "openai",
	"supports_function_calling": True,
	"supports_tool_choice": True,
	"supports_parallel_function_calling": True,
	"max_input_tokens": 200000,
	"max_output_tokens": 16384,
	"max_tokens": 216384,
}
logger.info("Successfully registered zai/glm-5 tool capability.")

# The scaleway/glm-5.1 and scaleway/glm-5.2 deployments route through
# `openai/<model>` (provider-agnostic OpenAI-compat path) -- not zai/.
# Register the openai/ aliases so `litellm.model_cost[model]` lookups in the
# ccproxy max_tokens clamp (which sees the proxy alias like "scaleway/glm-5.2"
# or the openai/ provider-prefixed name after route resolution) return the
# correct hard ceiling of 16384 output tokens. Without these entries the
# clamp falls through to a no-op and Claude Code's max_tokens=32768 request
# gets converted by litellm's OpenAI adapter into max_completion_tokens=32768,
# which scaleway's chat-completions endpoint rejects with a 400.
for _openai_glm_alias, _openai_glm_ctx in (
	("openai/glm-5.1", 200000),
	("openai/glm-5.2", 1000000),
):
	if _openai_glm_alias not in litellm.model_cost:
		litellm.model_cost[_openai_glm_alias] = {
			"mode": "chat",
			"litellm_provider": "openai",
			"supports_function_calling": True,
			"supports_tool_choice": True,
			"supports_parallel_function_calling": True,
			"max_input_tokens": _openai_glm_ctx,
			"max_output_tokens": 16384,
			"max_tokens": _openai_glm_ctx + 16384,
		}
		logger.info("Registered %s output cap (16384) for ccproxy max_tokens clamp.", _openai_glm_alias)

# LiteLLM refreshes model_cost from a remote JSON after module import, which
# wipes out ad-hoc entries for unmapped models like glm-5.1/5.2. Patch the
# info helper so Z.ai/GLM aliases are always recognised as tool-capable.
def _patch_zai_model_info():
	from litellm import utils as litellm_utils
	_original_get_model_info_helper = litellm_utils._get_model_info_helper

	def _zai_model_info(model: str, context_window: int, max_output_tokens: int) -> litellm_utils.ModelInfoBase:
		return litellm_utils.ModelInfoBase(
			key=model,
			max_tokens=context_window + max_output_tokens,
			max_input_tokens=context_window,
			max_output_tokens=max_output_tokens,
			input_cost_per_token=0,
			output_cost_per_token=0,
			litellm_provider="openai",
			mode="chat",
			supports_system_messages=None,
			supports_response_schema=None,
			supports_function_calling=True,
			supports_tool_choice=True,
			supports_assistant_prefill=None,
			supports_prompt_caching=None,
			supports_computer_use=None,
			supports_pdf_input=None,
			supports_parallel_function_calling=True,
			supports_reasoning=None,
		)

	def _wrapped_get_model_info_helper(model, custom_llm_provider=None, api_base=None, **kwargs):
		try:
			return _original_get_model_info_helper(model, custom_llm_provider, api_base, **kwargs)
		except Exception:
			model_lower = str(model or "").lower()
			if "glm-5.2" in model_lower or "glm_5.2" in model_lower:
				return _zai_model_info(model, context_window=1000000, max_output_tokens=16384)
			if "glm-5" in model_lower or "glm_5" in model_lower or "zai" in model_lower:
				return _zai_model_info(model, context_window=200000, max_output_tokens=16384)
			raise

	litellm_utils._get_model_info_helper = _wrapped_get_model_info_helper


	_patch_zai_model_info()
logger.info("Patched LiteLLM model info helper for Z.ai/GLM tool capability.")

# LiteLLM periodically refreshes litellm.model_cost from a remote JSON, which
# overwrites ad-hoc entries. For chatgpt/gpt-5.x the Codex backend only exposes
# the Responses API (/backend-api/codex/responses); the /chat/completions path
# is Cloudflare-challenged. We force the responses bridge in two places so
# /v1/messages and /v1/chat/completions both route through /responses even after
# a remote refresh.

def _patch_chatgpt_responses_bridge():
	from litellm import utils as litellm_utils

	orig_utils_helper = litellm_utils._get_model_info_helper

	def _chatgpt_fallback_info(model):
		return {
			"key": model,
			"mode": "responses",
			"litellm_provider": "chatgpt",
			"supports_function_calling": True,
			"supports_tool_choice": True,
			"supports_parallel_function_calling": True,
			"supports_native_streaming": True,
			"max_input_tokens": 1000000,
			"max_output_tokens": _CHATGPT_SAFE_MAX_OUTPUT_TOKENS,
			"max_tokens": 1000000 + _CHATGPT_SAFE_MAX_OUTPUT_TOKENS,
		}

	def _chatgpt_mode_forced_helper(model, custom_llm_provider=None, api_base=None, **kwargs):
		is_chatgpt = _CHATGPT_PATCHES_ENABLED and _is_chatgpt_model(
			model, custom_llm_provider
		)
		try:
			info = orig_utils_helper(model, custom_llm_provider, api_base, **kwargs)
		except Exception:
			info = None
		if info is None and is_chatgpt:
			info = _chatgpt_fallback_info(model)
		if info is not None and is_chatgpt:
			if isinstance(info, dict):
				info["mode"] = "responses"
				info["supports_native_streaming"] = True
			else:
				try:
					info.mode = "responses"
					info.supports_native_streaming = True
				except Exception:
					pass
		# Never return None: 1.91.1 builds ModelInfo(**info) for every model and
		# ModelInfo(**None) crashes responses_api_bridge_check for custom models.
		if info is None:
			return {}
		return info

	litellm_utils._get_model_info_helper = _chatgpt_mode_forced_helper

	# litellm.main imported _get_model_info_helper locally, so a utils patch alone
	# does not reach the responses bridge. Patch the main module reference too.
	try:
		import litellm.main as litellm_main

		litellm_main._get_model_info_helper = _chatgpt_mode_forced_helper

		orig_bridge = litellm_main.responses_api_bridge_check

		def _wrapped_bridge(model, custom_llm_provider, web_search_options=None, **kwargs):
			if _CHATGPT_PATCHES_ENABLED and _is_chatgpt_model(model, custom_llm_provider):
				return {"mode": "responses"}, model
			return orig_bridge(model, custom_llm_provider, web_search_options, **kwargs)

		litellm_main.responses_api_bridge_check = _wrapped_bridge
		logger.info(
			"Patched litellm.main responses bridge for provider-scoped ChatGPT models; enabled=%s.",
			_CHATGPT_PATCHES_ENABLED,
		)
	except Exception as e:
		logger.warning("Could not patch litellm.main responses bridge: %s", e)


_patch_chatgpt_responses_bridge()
logger.info(
	"Patched model info helper with provider-scoped ChatGPT Responses behavior; enabled=%s.",
	_CHATGPT_PATCHES_ENABLED,
)

# LiteLLM's Anthropic /v1/messages adapter strips the provider prefix (chatgpt/)
# before calling litellm.completion/acompletion, so chatgpt/gpt-5.5 becomes gpt-5.5
# and LiteLLM treats it as an OpenAI model. Re-prefix the model name in the
# adapter so the responses bridge and chatgpt authenticator are triggered.
def _patch_anthropic_messages_adapter_for_chatgpt():
	if not _CHATGPT_PATCHES_ENABLED:
		logger.info("ChatGPT Anthropic adapter patch disabled by CCPROXY_DISABLE_CHATGPT_PATCHES.")
		return
	try:
		from litellm.llms.anthropic.experimental_pass_through.messages import handler as anthropic_handler
		from litellm.llms.anthropic.experimental_pass_through.adapters.handler import (
			LiteLLMMessagesToCompletionTransformationHandler as Handler,
		)
	except Exception:
		return

	# Route ChatGPT models through the Responses API adapter (OpenAI-style) instead
	# of the chat/completion adapter. The completion adapter loses the provider
	# prefix and passes extra Router kwargs that break ChatGPT streaming; the
	# responses adapter keeps the request closer to the backend.
	orig_should_route = anthropic_handler._should_route_to_responses_api

	def _should_route(provider):
		if _CHATGPT_PATCHES_ENABLED and provider == "chatgpt":
			return True
		return orig_should_route(provider)

	anthropic_handler._should_route_to_responses_api = _should_route

	# Also patch the completion adapter to preserve the provider prefix if it is
	# ever used for ChatGPT.
	def _prefix_model(model, provider):
		if not provider or not isinstance(model, str):
			return model
		if "/" in model:
			return model
		return f"{provider}/{model}"

	_orig_async = Handler.async_anthropic_messages_handler

	async def _async_patched(*args, custom_llm_provider=None, **kwargs):
		if len(args) >= 3:
			args = list(args)
			args[2] = _prefix_model(args[2], custom_llm_provider)
		elif "model" in kwargs:
			kwargs["model"] = _prefix_model(kwargs["model"], custom_llm_provider)
		return await _orig_async(
			*args, custom_llm_provider=custom_llm_provider, **kwargs
		)

	Handler.async_anthropic_messages_handler = _async_patched

	_orig_sync = Handler.anthropic_messages_handler

	def _sync_patched(*args, custom_llm_provider=None, **kwargs):
		if len(args) >= 3:
			args = list(args)
			args[2] = _prefix_model(args[2], custom_llm_provider)
		elif "model" in kwargs:
			kwargs["model"] = _prefix_model(kwargs["model"], custom_llm_provider)
		return _orig_sync(*args, custom_llm_provider=custom_llm_provider, **kwargs)

	Handler.anthropic_messages_handler = _sync_patched


_patch_anthropic_messages_adapter_for_chatgpt()
logger.info("Patched Anthropic messages adapter to route chatgpt through Responses API.")


def _patch_chatgpt_fake_stream():
	if not _CHATGPT_PATCHES_ENABLED:
		return
	try:
		from litellm.llms.chatgpt.responses.transformation import ChatGPTResponsesAPIConfig
	except Exception:
		return

	orig_should_fake_stream = ChatGPTResponsesAPIConfig.should_fake_stream

	def _wrapped_should_fake_stream(self, model, stream, custom_llm_provider=None):
		if stream is not True:
			return False
		return orig_should_fake_stream(self, model, stream, custom_llm_provider)

	ChatGPTResponsesAPIConfig.should_fake_stream = _wrapped_should_fake_stream


_patch_chatgpt_fake_stream()
logger.info("Patched ChatGPT responses config to disable fake_stream for chatgpt/gpt-5.x.")


litellm.model_cost["scaleway/gpt-oss-120b"] = {
	"mode": "chat",
	"litellm_provider": "scaleway",
	"supports_function_calling": True,
	"supports_tool_choice": True,
	"supports_parallel_function_calling": True,
	"max_input_tokens": 200000,
	"max_output_tokens": 16384,
	"max_tokens": 216384,
}
logger.info("Successfully registered scaleway/gpt-oss-120b tool capability.")

litellm.model_cost["scaleway/devstral-2-123b-instruct-2512:fp8"] = {
	"mode": "chat",
	"litellm_provider": "scaleway",
	"supports_function_calling": True,
	"supports_tool_choice": True,
	"supports_parallel_function_calling": True,
	"max_input_tokens": 200000,
	"max_output_tokens": 16384,
	"max_tokens": 216384,
}
logger.info("Successfully registered scaleway/devstral-2-123b-instruct-2512:fp8 tool capability.")

litellm.model_cost["openai/gemma"] = {
	"mode": "chat",
	"litellm_provider": "openai",
	"supports_function_calling": True,
	"supports_tool_choice": True,
	"supports_parallel_function_calling": True,
	"max_input_tokens": 131072,
	"max_output_tokens": 8192,
	"max_tokens": 139264,
}
logger.info("Successfully registered openai/gemma tool capability.")

litellm.model_cost["openai/bielik"] = {
	"mode": "chat",
	"litellm_provider": "openai",
	"supports_function_calling": True,
	"supports_tool_choice": True,
	"supports_parallel_function_calling": True,
	"max_input_tokens": 16384,
	"max_output_tokens": 4096,
	"max_tokens": 20480,
}
logger.info("Successfully registered openai/bielik tool capability.")

def _register_chatgpt_responses_model(
	model: str,
	context_window: int = 1_050_000,
	max_output_tokens: int = 128_000,
) -> None:
	"""Keep Codex/ChatGPT capability metadata consistent across aliases."""
	max_output_tokens = min(max_output_tokens, _CHATGPT_SAFE_MAX_OUTPUT_TOKENS)
	litellm.model_cost[model] = {
		"mode": "responses",
		"litellm_provider": "chatgpt",
		"supports_function_calling": True,
		"supports_tool_choice": True,
		"supports_parallel_function_calling": True,
		"supports_native_streaming": True,
		"max_input_tokens": context_window,
		"max_output_tokens": max_output_tokens,
		"max_tokens": context_window + max_output_tokens,
	}
	logger.info("Registered %s as Responses-native ChatGPT tool model.", model)


for _chatgpt_model, _chatgpt_context, _chatgpt_output_cap in (
	("chatgpt/gpt-5.5", 1_000_000, 32768),
	("chatgpt/gpt-5.3-codex", 131072, 16384),
	("chatgpt/gpt-5.3-codex-spark", 131072, 16384),
	("chatgpt/gpt-5.6-sol", 1_050_000, 128000),
	("chatgpt/gpt-5.6-terra", 1_050_000, 128000),
	("chatgpt/gpt-5.6-luna", 1_050_000, 128000),
):
	_register_chatgpt_responses_model(
		_chatgpt_model,
		context_window=_chatgpt_context,
		max_output_tokens=_chatgpt_output_cap,
	)

# 2. Patch ChatGPT Authenticator to prevent interactive OAuth blocking during router init
try:
	from litellm.llms.chatgpt.authenticator import Authenticator

	def read_codex_auth(self):
		path = os.getenv("CODEX_AUTH_FILE", "/root/.codex/auth.json")
		try:
			with open(path, "r") as f:
				codex_auth = json.load(f)
		except (IOError, json.JSONDecodeError):
			return None

		tokens = codex_auth.get("tokens") or {}
		access_token = tokens.get("access_token")
		if not access_token:
			return None

		return {
			"access_token": access_token,
			"refresh_token": tokens.get("refresh_token"),
			"id_token": tokens.get("id_token"),
			"expires_at": self._get_expires_at(access_token),
			"account_id": tokens.get("account_id") or codex_auth.get("account_id"),
		}

	def patched_get_access_token(self) -> str:
		auth_data = self._read_auth_file() or read_codex_auth(self)
		if auth_data:
			access_token = auth_data.get("access_token")
			if access_token and not self._is_token_expired(auth_data, access_token):
				return access_token
			refresh_token = auth_data.get("refresh_token")
			if refresh_token:
				try:
					refreshed = self._refresh_tokens(refresh_token)
					return refreshed["access_token"]
				except Exception:
					pass
		raise RuntimeError(
			"ChatGPT subscription is not authenticated. Run Codex login on the host "
			"or set CHATGPT_TOKEN_DIR/CHATGPT_AUTH_FILE for LiteLLM."
		)

	def patched_get_account_id(self):
		auth_data = self._read_auth_file() or read_codex_auth(self)
		if not auth_data:
			return None
		account_id = auth_data.get("account_id")
		if account_id:
			return account_id
		return self._extract_account_id(auth_data.get("id_token") or auth_data.get("access_token"))

	Authenticator.get_access_token = patched_get_access_token
	Authenticator.get_account_id = patched_get_account_id
	logger.info("Successfully monkey-patched ChatGPT Authenticator to prevent interactive hangs.")
except Exception as e:
	logger.error(f"Failed to patch ChatGPT Authenticator: {e}", exc_info=True)

# 2b. Safeguard max_tokens calculation in token_counter to prevent negative max_tokens on fallback.
try:
	import litellm.litellm_core_utils.token_counter as _tc
	_orig_get_modified_max_tokens = _tc.get_modified_max_tokens
	def _safe_get_modified_max_tokens(*args, **kwargs):
		res = _orig_get_modified_max_tokens(*args, **kwargs)
		if res is not None and isinstance(res, (int, float)) and res < 1:
			logger.info(f"[sitecustomize] Safeguarding get_modified_max_tokens: {res} -> 1024")
			return 1024
		return res
	_tc.get_modified_max_tokens = _safe_get_modified_max_tokens
	try:
		import litellm._lazy_imports as _li
		_li._get_modified_max_tokens_func = _safe_get_modified_max_tokens
		_li._get_modified_max_tokens = lambda: _safe_get_modified_max_tokens
	except Exception:
		pass
	try:
		import litellm.utils as _lu
		_lu._get_modified_max_tokens_func = _safe_get_modified_max_tokens
		_lu._get_modified_max_tokens = lambda: _safe_get_modified_max_tokens
	except Exception:
		pass
	logger.info("Successfully monkey-patched token_counter.get_modified_max_tokens for fallback safety.")
except Exception as e:
	logger.error(f"Failed to patch token_counter get_modified_max_tokens: {e}", exc_info=True)

# 3. Harden Anthropic /v1/messages passthrough for gateway use.
try:
	from litellm.llms.anthropic.common_utils import ANTHROPIC_OAUTH_TOKEN_PREFIX
	from litellm.llms.anthropic import common_utils as anthropic_common_utils
	from litellm.llms.anthropic.experimental_pass_through.messages import (
		transformation as anthropic_messages_transformation,
	)
	from litellm.llms.anthropic.experimental_pass_through.adapters.transformation import (
		LiteLLMAnthropicMessagesAdapter,
	)
	from litellm.llms.anthropic.experimental_pass_through.adapters.handler import (
		LiteLLMMessagesToCompletionTransformationHandler,
	)

	_original_oauth_handler = anthropic_common_utils.optionally_handle_anthropic_oauth

	def _patched_optionally_handle_anthropic_oauth(headers, api_key):
		if (
			api_key
			and isinstance(api_key, str)
			and api_key.startswith(ANTHROPIC_OAUTH_TOKEN_PREFIX)
		):
			for key in list(headers):
				if isinstance(key, str) and key.lower() in {"authorization", "x-api-key"}:
					headers.pop(key, None)
		return _original_oauth_handler(headers, api_key)

	anthropic_common_utils.optionally_handle_anthropic_oauth = _patched_optionally_handle_anthropic_oauth
	anthropic_messages_transformation.optionally_handle_anthropic_oauth = _patched_optionally_handle_anthropic_oauth

	_original_translate_tools = LiteLLMAnthropicMessagesAdapter.translate_anthropic_tools_to_openai

	def _patched_translate_anthropic_tools_to_openai(self, tools, model=None):
		if isinstance(tools, list):
			openai_tools = []
			anthropic_tools = []
			for tool in tools:
				if not isinstance(tool, dict):
					continue
				function = tool.get("function")
				if tool.get("type") == "function" and isinstance(function, dict):
					name = function.get("name")
					if isinstance(name, str) and name.strip():
						coerced_function = dict(function)
						coerced_function["name"] = name.strip()
						coerced_function.setdefault(
							"parameters", {"type": "object", "properties": {}}
						)
						openai_tools.append({"type": "function", "function": coerced_function})
					continue
				if isinstance(tool.get("name"), str) and tool.get("name", "").strip():
					anthropic_tools.append(tool)
			if not anthropic_tools:
				return openai_tools, {}
			translated_tools, tool_name_mapping = _original_translate_tools(
				self, anthropic_tools, model
			)
			return openai_tools + translated_tools, tool_name_mapping
		return _original_translate_tools(self, tools, model)

	LiteLLMAnthropicMessagesAdapter.translate_anthropic_tools_to_openai = _patched_translate_anthropic_tools_to_openai

	_original_translate_anthropic_tool_choice_to_openai = (
		LiteLLMAnthropicMessagesAdapter.translate_anthropic_tool_choice_to_openai
	)

	def _patched_translate_anthropic_tool_choice_to_openai(self, tool_choice):
		if isinstance(tool_choice, dict) and tool_choice.get("type") == "function":
			function = tool_choice.get("function")
			if isinstance(function, dict):
				name = function.get("name")
				if isinstance(name, str) and name.strip():
					return {
						"type": "function",
						"function": {"name": name.strip()},
					}
		return _original_translate_anthropic_tool_choice_to_openai(self, tool_choice)

	LiteLLMAnthropicMessagesAdapter.translate_anthropic_tool_choice_to_openai = (
		_patched_translate_anthropic_tool_choice_to_openai
	)

	_original_translate_anthropic_to_openai = LiteLLMAnthropicMessagesAdapter.translate_anthropic_to_openai

	def _single_openai_tool_name(tools):
		if not isinstance(tools, list) or len(tools) != 1:
			return None
		tool = tools[0]
		function = tool.get("function") if isinstance(tool, dict) else getattr(tool, "function", None)
		if not isinstance(function, dict):
			return None
		name = function.get("name")
		return name if isinstance(name, str) and name.strip() else None

	def _harden_fallback_tool_routing(completion_kwargs):
		if not completion_kwargs.get("tools"):
			return
		# For single-tool requests, force the model to emit a structured tool call
		# rather than describing or printing JSON. Live probes of the Z.ai/GLM
		# coding endpoint accept named function tool_choice, so keep Z.ai on the
		# same path as the other non-Anthropic providers.
		tool_name = _single_openai_tool_name(completion_kwargs.get("tools"))
		tool_choice = completion_kwargs.get("tool_choice")
		if tool_name and tool_choice in (None, "auto", "any", "required"):
			completion_kwargs["tool_choice"] = {
				"type": "function",
				"function": {"name": tool_name},
			}
		messages = completion_kwargs.get("messages")
		if not isinstance(messages, list):
			return
		routing_prompt = (
			"Tool-use routing requirement: when tool_choice names or requires a tool, "
			"return a structured OpenAI tool_calls response for that tool. Do not "
			"execute the tool mentally, do not print inferred command output, and do "
			"not describe the tool call. If native tool_calls cannot be emitted, return "
			'only a JSON object shaped as {"tool":"<tool name>","arguments":{...}}.'
		)
		if any(
			isinstance(message, dict)
			and message.get("role") == "system"
			and message.get("content") == routing_prompt
			for message in messages
		):
			return
		completion_kwargs["messages"] = [{"role": "system", "content": routing_prompt}] + messages

	def _patched_translate_anthropic_to_openai(self, anthropic_message_request):
		new_kwargs, tool_name_mapping = _original_translate_anthropic_to_openai(
			self, anthropic_message_request
		)
		model = str(new_kwargs.get("model") or "")
		if (
			model in {"kimi", "moonshot/kimi-for-coding"}
			or model.startswith("moonshot/")
			or model.startswith("deepseek")
		):
			tool_name = _single_openai_tool_name(new_kwargs.get("tools"))
			tool_choice = new_kwargs.get("tool_choice")
			if tool_name and tool_choice in (None, "auto", "any", "required"):
				new_kwargs["tool_choice"] = {
					"type": "function",
					"function": {"name": tool_name},
				}
		_harden_fallback_tool_routing(new_kwargs)
		return new_kwargs, tool_name_mapping

	LiteLLMAnthropicMessagesAdapter.translate_anthropic_to_openai = _patched_translate_anthropic_to_openai

	_original_prepare_completion_kwargs = (
		LiteLLMMessagesToCompletionTransformationHandler._prepare_completion_kwargs
	)

	def _patched_prepare_completion_kwargs(*args, **kwargs):
		completion_kwargs, tool_name_mapping = _original_prepare_completion_kwargs(
			*args, **kwargs
		)
		model = str(completion_kwargs.get("model") or "")
		if (
			model in {"kimi", "moonshot/kimi-for-coding"}
			or model.startswith("moonshot/")
			or model.startswith("deepseek")
		):
			tool_name = _single_openai_tool_name(completion_kwargs.get("tools"))
			tool_choice = completion_kwargs.get("tool_choice")
			if tool_name and tool_choice in (None, "auto", "any", "required"):
				completion_kwargs["tool_choice"] = {
					"type": "function",
					"function": {"name": tool_name},
					}
		_harden_fallback_tool_routing(completion_kwargs)
		return completion_kwargs, tool_name_mapping

	LiteLLMMessagesToCompletionTransformationHandler._prepare_completion_kwargs = staticmethod(
		_patched_prepare_completion_kwargs
	)

	_original_translate_openai_content_to_anthropic = (
		LiteLLMAnthropicMessagesAdapter._translate_openai_content_to_anthropic
	)

	def _get_field(obj, key):
		if isinstance(obj, dict):
			return obj.get(key)
		return getattr(obj, key, None)

	def _coerce_tool_arguments(value):
		if isinstance(value, dict):
			return value
		if isinstance(value, str) and value.strip():
			try:
				parsed = json.loads(value)
			except Exception:
				return {}
			return parsed if isinstance(parsed, dict) else {}
		return {}

	def _strip_json_markdown_wrapper(value):
		text = value.strip()
		if text.startswith("```") and text.endswith("```"):
			lines = text.splitlines()
			if len(lines) >= 2 and lines[-1].strip() == "```":
				return "\n".join(lines[1:-1]).strip()
		if text.startswith("`") and text.endswith("`") and "\n" not in text:
			return text[1:-1].strip()
		return text

	def _canonical_tool_name(name, tool_name_mapping):
		raw_name = name.strip()
		if not raw_name:
			return ""
		candidates = []
		if isinstance(tool_name_mapping, dict):
			for translated_name, original_name in tool_name_mapping.items():
				if isinstance(original_name, str) and original_name.strip():
					candidates.append((original_name, original_name))
				if isinstance(translated_name, str) and translated_name.strip():
					candidates.append((translated_name, original_name))
		for candidate, original_name in candidates:
			if raw_name == candidate and isinstance(original_name, str) and original_name.strip():
				return original_name
		for candidate, original_name in candidates:
			if (
				raw_name.lower() == candidate.lower()
				and isinstance(original_name, str)
				and original_name.strip()
			):
				return original_name
		known_claude_tools = {
			"agent": "Agent",
			"bash": "Bash",
			"edit": "Edit",
			"glob": "Glob",
			"grep": "Grep",
			"ls": "LS",
			"mcp": "mcp",
			"multiedit": "MultiEdit",
			"notebookedit": "NotebookEdit",
			"read": "Read",
			"todowrite": "TodoWrite",
			"webfetch": "WebFetch",
			"websearch": "WebSearch",
			"write": "Write",
		}
		return known_claude_tools.get(raw_name.lower(), raw_name)

	def _merge_adjacent_text_blocks(blocks):
		if not isinstance(blocks, list):
			return blocks
		merged = []
		for block in blocks:
			if not isinstance(block, dict):
				merged.append(block)
				continue
			if (
				block.get("type") == "text"
				and merged
				and isinstance(merged[-1], dict)
				and merged[-1].get("type") == "text"
			):
				prev_txt = merged[-1].get("text", "")
				curr_txt = block.get("text", "")
				if prev_txt and curr_txt:
					merged[-1]["text"] = prev_txt + "\n\n" + curr_txt
				elif curr_txt:
					merged[-1]["text"] = curr_txt
			else:
				merged.append(dict(block))
		return merged

	def _format_reasoning_as_blockquote(text):
		if not isinstance(text, str) or not text.strip():
			return ""
		lines = text.strip().splitlines()
		formatted = [f"> *{line}*" if line.strip() else ">" for line in lines]
		return "> **Reasoning:**\n" + "\n".join(formatted) + "\n\n"

	def _serialized_tool_text_to_tool_use(text, tool_name_mapping):
		# Some fallback providers return a serialized tool call as their whole text response.
		if not isinstance(text, str) or not text.strip():
			return None
		try:
			parsed = json.loads(_strip_json_markdown_wrapper(text))
		except Exception:
			return None
		# Accept a JSON array of calls, a single call dict, or a wrapper object
		# with a "tool_calls" key (common when models emit raw OpenAI-style JSON).
		if isinstance(parsed, list) and parsed:
			parsed = parsed[0]
		if not isinstance(parsed, dict):
			return None
		tool_calls = parsed.get("tool_calls")
		if isinstance(tool_calls, list) and tool_calls:
			parsed = tool_calls[0]
			if isinstance(parsed, dict):
				function = parsed.get("function")
				if isinstance(function, dict):
					name = function.get("name")
					arguments = function.get("arguments")
					if isinstance(name, str) and name.strip():
						from uuid import uuid4

						return {
							"type": "tool_use",
							"id": f"toolu_{uuid4().hex}",
							"name": _canonical_tool_name(name, tool_name_mapping),
							"input": _coerce_tool_arguments(arguments),
						}
			return None
		function = parsed.get("function")
		if isinstance(function, dict):
			name = function.get("name") or parsed.get("name") or parsed.get("tool")
			arguments = function.get("arguments")
		else:
			name = parsed.get("name") or parsed.get("tool")
			arguments = parsed.get("arguments")
		if arguments is None:
			arguments = parsed.get("input")
		if not isinstance(name, str) or not name.strip():
			return None
		from uuid import uuid4

		return {
			"type": "tool_use",
			"id": f"toolu_{uuid4().hex}",
			"name": _canonical_tool_name(name, tool_name_mapping),
			"input": _coerce_tool_arguments(arguments),
		}

	def _parse_minimax_tool_call_text(text, tool_name_mapping=None):
		if not isinstance(text, str) or not any(k in text for k in ("<tool_call>", "<minimax:tool_call>", "]<]minimax[>[", "<invoke")):
			return text, []
		import re
		from uuid import uuid4

		pattern = r"(?:\]<\]minimax\[>\[|<minimax:tool_call>)?<tool_call>(.*?)(?:\]<\]minimax\[>\[|</minimax:tool_call>)?(?:</tool_call>|$)"
		matches = list(re.finditer(pattern, text, re.DOTALL))
		if not matches:
			matches = list(re.finditer(r"(?:\]<\]minimax\[>\[)?<invoke name=\"[^\"]+\">.*?(?:</invoke>|$)", text, re.DOTALL))
			if not matches:
				return text, []

		tool_blocks = []
		spans = []

		for match in matches:
			spans.append((match.start(), match.end()))
			body = match.group(0)
			invoke_pattern = r"(?:\]<\]minimax\[>\[)?<invoke name=\"([^\"]+)\">(.*?)(?:\]<\]minimax\[>\[)?(?:</invoke>|$)"
			for inv in re.finditer(invoke_pattern, body, re.DOTALL):
				raw_tool_name = inv.group(1).strip()
				inv_body = inv.group(2)

				params = {}
				for p in re.finditer(r"<parameter name=\"([^\"]+)\">(.*?)</parameter>", inv_body, re.DOTALL):
					params[p.group(1).strip()] = p.group(2).strip()

				for p in re.finditer(r"(?:\]<\]minimax\[>\[)?<([a-zA-Z0-9_]+)>(.*?)(?:\]<\]minimax\[>\[)?</\1>", inv_body, re.DOTALL):
					k = p.group(1).strip()
					v = p.group(2).strip()
					if k not in ("invoke", "parameter", "tool_call", "minimax"):
						params[k] = v

				cleaned_params = {}
				for k, v in params.items():
					clean_v = re.sub(r"\]<\]minimax\[>\[|\]<\]minimax\[>\]", "", v).strip()
					if clean_v:
						cleaned_params[k] = clean_v

				# Fail-safe for Bash tool: command parameter is mandatory for Claude Code
				if raw_tool_name.lower() in ("bash", "execute_command", "command"):
					if "command" not in cleaned_params:
						if "cmd" in cleaned_params:
							cleaned_params["command"] = cleaned_params.pop("cmd")
						elif "description" in cleaned_params and any(cleaned_params["description"].startswith(prefix) for prefix in ("docker", "ls", "cd", "cat", "grep", "git", "gh", "curl", "npm", "python", "echo", "sleep")):
							cleaned_params["command"] = cleaned_params["description"]
						else:
							raw_text = re.sub(r"<[^>]+>|\]<\]minimax\[>\[|\]<\]minimax\[>\]", " ", inv_body).strip()
							if raw_text:
								cleaned_params["command"] = raw_text

				canonical_name = _canonical_tool_name(raw_tool_name, tool_name_mapping) if tool_name_mapping else raw_tool_name
				if canonical_name and (cleaned_params or canonical_name.lower() == "bash"):
					tool_id = f"toolu_minimax_{uuid4().hex[:12]}"
					tool_blocks.append({
						"type": "tool_use",
						"id": tool_id,
						"name": canonical_name,
						"input": cleaned_params
					})

		cleaned = text
		for start, end in reversed(spans):
			cleaned = cleaned[:start] + cleaned[end:]

		cleaned = re.sub(r"\]<\]minimax\[>\[\s*", "", cleaned).strip()
		return cleaned, tool_blocks

	def _serialized_tool_text_blocks(content, tool_name_mapping):
		tool_blocks = []
		drop_indexes = set()
		for index, block in enumerate(content or []):
			if not isinstance(block, dict) or block.get("type") != "text":
				continue
			raw_text = block.get("text")
			cleaned_text, mm_tools = _parse_minimax_tool_call_text(raw_text, tool_name_mapping)
			if mm_tools:
				tool_blocks.extend(mm_tools)
				if cleaned_text:
					block["text"] = cleaned_text
				else:
					drop_indexes.add(index)
				continue
			tool_block = _serialized_tool_text_to_tool_use(raw_text, tool_name_mapping)
			if tool_block:
				tool_blocks.append(tool_block)
				drop_indexes.add(index)
		return tool_blocks, drop_indexes

	def _patched_translate_openai_content_to_anthropic(
		self,
		choices,
		tool_name_mapping=None,
	):
		content = _original_translate_openai_content_to_anthropic(
			self,
			choices=choices,
			tool_name_mapping=tool_name_mapping,
		)

		tool_blocks = []
		for choice in choices or []:
			message = _get_field(choice, "message")
			if message is None:
				continue
			tool_calls = _get_field(message, "tool_calls")
			if not isinstance(tool_calls, list):
				continue
			for tool_call in tool_calls:
				function = _get_field(tool_call, "function")
				if function is None:
					continue
				name = _get_field(function, "name")
				if not isinstance(name, str) or not name.strip():
					continue
				original_name = (
					tool_name_mapping.get(name, name)
					if isinstance(tool_name_mapping, dict)
					else name
				)
				tool_id = _get_field(tool_call, "id")
				if not isinstance(tool_id, str) or not tool_id.strip():
					from uuid import uuid4

					tool_id = f"toolu_{uuid4().hex}"
				tool_blocks.append(
					{
						"type": "tool_use",
						"id": tool_id,
						"name": original_name,
						"input": _coerce_tool_arguments(_get_field(function, "arguments")),
					}
				)

		if not tool_blocks:
			tool_blocks, drop_indexes = _serialized_tool_text_blocks(content, tool_name_mapping)
		else:
			drop_indexes = set()

		real_content = [
			block
			for index, block in enumerate(content)
			if not (
				index in drop_indexes
				or (
					isinstance(block, dict)
					and block.get("type") == "text"
					and not str(block.get("text") or "").strip()
				)
			)
		]

		final_content = []
		for block in real_content:
			if isinstance(block, dict) and block.get("type") == "thinking":
				thinking_text = block.get("thinking", "")
				if thinking_text and thinking_text.strip():
					final_content.append({
						"type": "text",
						"text": _format_reasoning_as_blockquote(thinking_text),
					})
			else:
				final_content.append(block)
		result_blocks = _merge_adjacent_text_blocks(final_content + tool_blocks)
		if not result_blocks:
			rc_text = None
			for choice in choices or []:
				msg = _get_field(choice, "message")
				if msg is not None:
					rc_text = _get_field(msg, "reasoning_content") or getattr(msg, "reasoning_content", None)
					if not rc_text and isinstance(_get_field(msg, "provider_specific_fields"), dict):
						rc_text = _get_field(msg, "provider_specific_fields").get("reasoning")
					if rc_text and str(rc_text).strip():
						rc_text = str(rc_text).strip()
						break
			if rc_text:
				result_blocks = [{"type": "text", "text": rc_text}]
			else:
				result_blocks = [{"type": "text", "text": ""}]
		return result_blocks

	LiteLLMAnthropicMessagesAdapter._translate_openai_content_to_anthropic = (
		_patched_translate_openai_content_to_anthropic
	)

	_original_translate_openai_response_to_anthropic = (
		LiteLLMAnthropicMessagesAdapter.translate_openai_response_to_anthropic
	)

	def _patched_translate_openai_response_to_anthropic(self, response, tool_name_mapping=None, polyfill_result=None):
		translated = _original_translate_openai_response_to_anthropic(
			self, response=response, tool_name_mapping=tool_name_mapping, polyfill_result=polyfill_result
		)
		if hasattr(translated, "content") and isinstance(translated.content, list):
			new_content = []
			for block in translated.content:
				if isinstance(block, dict) and block.get("type") == "thinking":
					thinking_text = block.get("thinking", "")
					if thinking_text and thinking_text.strip():
						new_content.append({
							"type": "text",
							"text": _format_reasoning_as_blockquote(thinking_text),
						})
				else:
					new_content.append(block)
			if not new_content:
				new_content.append({"type": "text", "text": ""})
			translated.content = _merge_adjacent_text_blocks(new_content)
		elif isinstance(translated, dict) and isinstance(translated.get("content"), list):
			new_content = []
			for block in translated["content"]:
				if isinstance(block, dict) and block.get("type") == "thinking":
					thinking_text = block.get("thinking", "")
					if thinking_text and thinking_text.strip():
						new_content.append({
							"type": "text",
							"text": _format_reasoning_as_blockquote(thinking_text),
						})
				else:
					new_content.append(block)
			if not new_content:
				new_content.append({"type": "text", "text": ""})
			translated["content"] = _merge_adjacent_text_blocks(new_content)
		return translated

	LiteLLMAnthropicMessagesAdapter.translate_openai_response_to_anthropic = (
		_patched_translate_openai_response_to_anthropic
	)

	import litellm
	import litellm.proxy.anthropic_endpoints.endpoints as _endpoints

	_orig_strip_func = getattr(_endpoints, "_strip_total_tokens_from_anthropic_response", None)

	def _patched_strip_total_tokens(response):
		if _orig_strip_func:
			try:
				_orig_strip_func(response)
			except Exception:
				pass
		
		if response is None:
			return
		
		content = None
		if isinstance(response, dict):
			content = response.get("content")
		elif hasattr(response, "content"):
			content = getattr(response, "content")
			
		if isinstance(content, list):
			new_content = []
			for block in content:
				if isinstance(block, dict) and block.get("type") == "thinking":
					txt = block.get("thinking", "")
					if txt and str(txt).strip():
						new_content.append({"type": "text", "text": _format_reasoning_as_blockquote(str(txt))})
				else:
					new_content.append(block)
			new_content = _merge_adjacent_text_blocks(new_content)
			if isinstance(response, dict):
				response["content"] = new_content
			else:
				try:
					setattr(response, "content", new_content)
				except Exception:
					pass

	_endpoints._strip_total_tokens_from_anthropic_response = _patched_strip_total_tokens
	litellm.strip_anthropic_total_tokens = True

	logger.info("Patched Anthropic passthrough auth/tool/thinking translation for LiteLLM gateway use.")
except Exception as e:
	logger.error(f"Failed to patch Anthropic passthrough handling: {e}", exc_info=True)

# 3b. Prefetch the first transformed non-Anthropic /v1/messages stream chunk.
#
#     LiteLLM's generic router treats a streaming response object as success
#     before any provider bytes have arrived. That means zero-first-byte stalls
#     in non-Anthropic Claude Code fallback models bypass router fallbacks. Pull
#     the first provider-derived transformed chunk inside the adapter call, so
#     timeouts raise before the router declares success. The adapter emits
#     synthetic message_start/content_block_start scaffolding before reading
#     provider bytes; those chunks do not prove the upstream is responsive.
try:
	import asyncio
	from litellm.llms.anthropic.experimental_pass_through.adapters.handler import (
		LiteLLMMessagesToCompletionTransformationHandler,
	)

	_original_async_anthropic_messages_handler = (
		LiteLLMMessagesToCompletionTransformationHandler.async_anthropic_messages_handler
	)

	def _stream_requested(args, kwargs):
		if "stream" in kwargs:
			return kwargs.get("stream") is True
		return len(args) > 5 and args[5] is True

	def _first_chunk_timeout(kwargs):
		value = kwargs.get("stream_timeout")
		if value is None:
			value = kwargs.get("timeout")
		try:
			return float(value) if value is not None else None
		except (TypeError, ValueError):
			return None

	def _model_from_call(args, kwargs):
		return str(kwargs.get("model") or (args[2] if len(args) > 2 else ""))

	def _is_synthetic_adapter_chunk(chunk):
		if not isinstance(chunk, dict):
			return False
		chunk_type = chunk.get("type")
		if chunk_type == "message_start":
			return True
		if chunk_type != "content_block_start":
			return False
		if chunk.get("index") not in (None, 0):
			return False
		content_block = chunk.get("content_block")
		return (
			isinstance(content_block, dict)
			and content_block.get("type") == "text"
			and content_block.get("text", "") == ""
		)

	async def _yield_prefetched_stream(prefetched_chunks, iterator):
		for chunk in prefetched_chunks:
			yield chunk
		async for chunk in iterator:
			yield chunk

	def _is_async_iterable_stream(result):
		return callable(getattr(result, "__aiter__", None))

	def _default_first_chunk_timeout():
		try:
			return float(os.getenv("ANTHROPIC_MESSAGES_FIRST_CHUNK_TIMEOUT", "30"))
		except (TypeError, ValueError):
			return 30.0

	def _effective_first_chunk_timeout(kwargs):
		default_timeout = _default_first_chunk_timeout()
		configured_timeout = _first_chunk_timeout(kwargs)
		if configured_timeout is None or configured_timeout <= 0:
			return default_timeout
		if default_timeout is None or default_timeout <= 0:
			return configured_timeout
		return min(configured_timeout, default_timeout)

	async def _patched_async_anthropic_messages_handler(*args, **kwargs):
		stream_requested = _stream_requested(args, kwargs)
		timeout = _effective_first_chunk_timeout(kwargs) if stream_requested else None
		model = _model_from_call(args, kwargs)
		try:
			if stream_requested and timeout is not None and timeout > 0:
				result = await asyncio.wait_for(
					_original_async_anthropic_messages_handler(*args, **kwargs),
					timeout=timeout,
				)
			else:
				result = await _original_async_anthropic_messages_handler(*args, **kwargs)
		except asyncio.TimeoutError as exc:
			logger.warning(
				"[STREAM_TIMEOUT_GUARD] adapter call timeout model=%s timeout=%s",
				model,
				timeout,
			)
			raise litellm.Timeout(
				message=(
					f"Anthropic /v1/messages adapter returned no stream object "
					f"within {timeout}s"
				),
				model=model,
				llm_provider="anthropic_messages_adapter",
			) from exc
		if not stream_requested:
			return result
		if not _is_async_iterable_stream(result):
			return result
		if timeout is None or timeout <= 0:
			return result
		iterator = result.__aiter__()
		prefetched_chunks = []
		logger.debug(
			"[STREAM_TIMEOUT_GUARD] armed model=%s timeout=%s kwargs=%s",
			model,
			timeout,
			sorted(kwargs.keys()),
		)
		try:
			while True:
				chunk = await asyncio.wait_for(iterator.__anext__(), timeout=timeout)
				prefetched_chunks.append(chunk)
				if not _is_synthetic_adapter_chunk(chunk):
					logger.debug(
						"[STREAM_TIMEOUT_GUARD] provider chunk ready model=%s "
						"prefetched=%s chunk_type=%s",
						model,
						len(prefetched_chunks),
						chunk.get("type") if isinstance(chunk, dict) else type(chunk).__name__,
					)
					break
		except StopAsyncIteration:
			raise litellm.Timeout(
				message=(
					"Anthropic /v1/messages adapter stream ended before a "
					"provider-derived chunk"
				),
				model=model,
				llm_provider="anthropic_messages_adapter",
			)
		except asyncio.TimeoutError as exc:
			logger.warning(
				"[STREAM_TIMEOUT_GUARD] provider first chunk timeout model=%s "
				"timeout=%s prefetched=%s",
				model,
				timeout,
				len(prefetched_chunks),
			)
			raise litellm.Timeout(
				message=(
					f"Anthropic /v1/messages adapter received no provider-derived "
					f"stream chunk within {timeout}s"
				),
				model=model,
				llm_provider="anthropic_messages_adapter",
			) from exc
		return _yield_prefetched_stream(prefetched_chunks, iterator)

	LiteLLMMessagesToCompletionTransformationHandler.async_anthropic_messages_handler = (
		staticmethod(_patched_async_anthropic_messages_handler)
	)
	logger.info("Patched Anthropic messages adapter to prefetch first stream chunk.")
except Exception as e:
	logger.error(f"Failed to patch Anthropic messages first-chunk prefetch: {e}", exc_info=True)

# 4. LiteLLM 1.82.x can drop the first `input_json_delta` when an OpenAI-style
#    stream switches from a text block to a tool_use block. The tool starts with
#    `input={}` and Claude Code fails the next turn with missing required args.
#    Queue the trigger delta immediately after content_block_start, matching the
#    Anthropic streaming contract.
try:
	import traceback
	from litellm import verbose_logger
	from litellm._uuid import uuid
	from litellm.llms.anthropic.experimental_pass_through.adapters.streaming_iterator import (
		AnthropicStreamWrapper,
	)

	def _queue_content_block_switch(self, processed_chunk):
		self.chunk_queue.append(
			{
				"type": "content_block_stop",
				"index": max(self.current_content_block_index - 1, 0),
			}
		)
		self.chunk_queue.append(
			{
				"type": "content_block_start",
				"index": self.current_content_block_index,
				"content_block": self.current_content_block_start,
			}
		)
		if (
			isinstance(processed_chunk, dict)
			and processed_chunk.get("type") == "content_block_delta"
		):
			self.chunk_queue.append(processed_chunk)
		self.sent_content_block_finish = False

	from litellm.llms.anthropic.experimental_pass_through.adapters.transformation import (
		LiteLLMAnthropicMessagesAdapter,
	)

	_original_translate_streaming_openai_chunk_to_anthropic_content_block = (
		LiteLLMAnthropicMessagesAdapter._translate_streaming_openai_chunk_to_anthropic_content_block
	)

	def _patched_translate_streaming_openai_chunk_to_anthropic_content_block(self, choices):
		for choice in choices:
			if (
				choice.delta.tool_calls is not None
				and len(choice.delta.tool_calls) > 0
				and choice.delta.tool_calls[0].function is not None
			):
				pass
			elif choice.delta.content is not None and len(choice.delta.content) > 0:
				pass
			elif hasattr(choice.delta, "reasoning_content") and choice.delta.reasoning_content is not None and len(choice.delta.reasoning_content) > 0:
				from litellm.types.llms.anthropic import TextBlock
				return "text", TextBlock(
					type="text", text=choice.delta.reasoning_content
				)

		return _original_translate_streaming_openai_chunk_to_anthropic_content_block(self, choices)

	LiteLLMAnthropicMessagesAdapter._translate_streaming_openai_chunk_to_anthropic_content_block = (
		_patched_translate_streaming_openai_chunk_to_anthropic_content_block
	)

	_original_translate_streaming_openai_response_to_anthropic = (
		LiteLLMAnthropicMessagesAdapter.translate_streaming_openai_response_to_anthropic
	)

	def _patched_translate_streaming_openai_response_to_anthropic(self, response, current_content_block_index):
		res = _original_translate_streaming_openai_response_to_anthropic(self, response, current_content_block_index)
		if isinstance(res, dict) and res.get("type") == "content_block_delta":
			delta = res.get("delta") or {}
			if delta.get("type") == "thinking_delta":
				res["delta"] = {
					"type": "text_delta",
					"text": delta.get("thinking", ""),
				}
			elif delta.get("type") == "signature_delta":
				res = {"type": "ping"}
		return res

	LiteLLMAnthropicMessagesAdapter.translate_streaming_openai_response_to_anthropic = (
		_patched_translate_streaming_openai_response_to_anthropic
	)

	def _has_actual_content(chunk) -> bool:
		if not hasattr(chunk, "choices") or not chunk.choices:
			return False
		for choice in chunk.choices:
			if not hasattr(choice, "delta") or not choice.delta:
				continue
			if getattr(choice.delta, "content", None) is not None and len(choice.delta.content) > 0:
				return True
			if getattr(choice.delta, "tool_calls", None) is not None and len(choice.delta.tool_calls) > 0:
				return True
			if getattr(choice.delta, "reasoning_content", None) is not None and len(choice.delta.reasoning_content) > 0:
				return True
			if getattr(choice.delta, "thinking_blocks", None) is not None and len(choice.delta.thinking_blocks) > 0:
				return True
		return False

	def _patched_anthropic_stream_next(self):
		try:
			if self.chunk_queue:
				return self.chunk_queue.popleft()

			if self.sent_first_chunk is False:
				self.sent_first_chunk = True
				self.chunk_queue.append(
					{
						"type": "message_start",
						"message": {
							"id": "msg_{}".format(uuid.uuid4()),
							"type": "message",
							"role": "assistant",
							"content": [],
							"model": self.model,
							"stop_reason": None,
							"stop_sequence": None,
							"usage": self._create_initial_usage_delta(),
						},
					}
				)
				return self.chunk_queue.popleft()

			for chunk in self.completion_stream:
				if chunk == "None" or chunk is None:
					raise Exception

				if self.sent_content_block_start is False:
					if not _has_actual_content(chunk):
						continue
					self.sent_content_block_start = True
					block_type, content_block_start = LiteLLMAnthropicMessagesAdapter()._translate_streaming_openai_chunk_to_anthropic_content_block(
						choices=chunk.choices
					)
					self.current_content_block_type = block_type
					self.current_content_block_start = content_block_start
					self.chunk_queue.append(
						{
							"type": "content_block_start",
							"index": self.current_content_block_index,
							"content_block": self.current_content_block_start,
						}
					)
					processed_chunk = LiteLLMAnthropicMessagesAdapter().translate_streaming_openai_response_to_anthropic(
						response=chunk,
						current_content_block_index=self.current_content_block_index,
					)
					self.chunk_queue.append(processed_chunk)
					return self.chunk_queue.popleft()

				should_start_new_block = self._should_start_new_content_block(chunk)
				if should_start_new_block:
					self._increment_content_block_index()

				processed_chunk = LiteLLMAnthropicMessagesAdapter().translate_streaming_openai_response_to_anthropic(
					response=chunk,
					current_content_block_index=self.current_content_block_index,
				)

				if should_start_new_block and not self.sent_content_block_finish:
					_queue_content_block_switch(self, processed_chunk)
					return self.chunk_queue.popleft()

				if (
					processed_chunk["type"] == "message_delta"
					and self.sent_content_block_start is True
					and self.sent_content_block_finish is False
				):
					self.chunk_queue.append(
						{
							"type": "content_block_stop",
							"index": self.current_content_block_index,
						}
					)
					self.sent_content_block_finish = True
					self.chunk_queue.append(processed_chunk)
					return self.chunk_queue.popleft()
				elif self.holding_chunk is not None:
					self.chunk_queue.append(self.holding_chunk)
					self.chunk_queue.append(processed_chunk)
					self.holding_chunk = None
					return self.chunk_queue.popleft()
				else:
					self.chunk_queue.append(processed_chunk)
					return self.chunk_queue.popleft()

			if self.holding_chunk is not None:
				self.chunk_queue.append(self.holding_chunk)
				self.holding_chunk = None

			if not self.sent_last_message:
				self.sent_last_message = True
				self.chunk_queue.append({"type": "message_stop"})

			if self.chunk_queue:
				return self.chunk_queue.popleft()

			raise StopIteration
		except StopIteration:
			if self.chunk_queue:
				return self.chunk_queue.popleft()
			if self.sent_last_message is False:
				self.sent_last_message = True
				return {"type": "message_stop"}
			raise StopIteration
		except Exception as e:
			verbose_logger.error(
				"Anthropic Adapter - {}\n{}".format(e, traceback.format_exc())
			)
			raise StopAsyncIteration

	async def _patched_anthropic_stream_anext(self):
		try:
			if self.chunk_queue:
				return self.chunk_queue.popleft()

			if self.sent_first_chunk is False:
				self.sent_first_chunk = True
				self.chunk_queue.append(
					{
						"type": "message_start",
						"message": {
							"id": "msg_{}".format(uuid.uuid4()),
							"type": "message",
							"role": "assistant",
							"content": [],
							"model": self.model,
							"stop_reason": None,
							"stop_sequence": None,
							"usage": self._create_initial_usage_delta(),
						},
					}
				)
				return self.chunk_queue.popleft()

			async for chunk in self.completion_stream:
				if chunk == "None" or chunk is None:
					raise Exception

				if self.sent_content_block_start is False:
					if not _has_actual_content(chunk):
						continue
					self.sent_content_block_start = True
					block_type, content_block_start = LiteLLMAnthropicMessagesAdapter()._translate_streaming_openai_chunk_to_anthropic_content_block(
						choices=chunk.choices
					)
					self.current_content_block_type = block_type
					self.current_content_block_start = content_block_start
					self.chunk_queue.append(
						{
							"type": "content_block_start",
							"index": self.current_content_block_index,
							"content_block": self.current_content_block_start,
						}
					)
					processed_chunk = LiteLLMAnthropicMessagesAdapter().translate_streaming_openai_response_to_anthropic(
						response=chunk,
						current_content_block_index=self.current_content_block_index,
					)
					self.chunk_queue.append(processed_chunk)
					return self.chunk_queue.popleft()

				should_start_new_block = self._should_start_new_content_block(chunk)
				if should_start_new_block:
					self._increment_content_block_index()

				processed_chunk = LiteLLMAnthropicMessagesAdapter().translate_streaming_openai_response_to_anthropic(
					response=chunk,
					current_content_block_index=self.current_content_block_index,
				)

				if (
					self.holding_stop_reason_chunk is not None
					and getattr(chunk, "usage", None) is not None
				):
					merged_chunk = self.holding_stop_reason_chunk.copy()
					if "delta" not in merged_chunk:
						merged_chunk["delta"] = {}

					uncached_input_tokens = chunk.usage.prompt_tokens or 0
					if (
						hasattr(chunk.usage, "prompt_tokens_details")
						and chunk.usage.prompt_tokens_details
					):
						cached_tokens = (
							getattr(
								chunk.usage.prompt_tokens_details,
								"cached_tokens",
								0,
							)
							or 0
						)
						uncached_input_tokens -= cached_tokens

					usage_dict = {
						"input_tokens": uncached_input_tokens,
						"output_tokens": chunk.usage.completion_tokens or 0,
					}
					if (
						hasattr(chunk.usage, "_cache_creation_input_tokens")
						and chunk.usage._cache_creation_input_tokens > 0
					):
						usage_dict[
							"cache_creation_input_tokens"
						] = chunk.usage._cache_creation_input_tokens
					if (
						hasattr(chunk.usage, "_cache_read_input_tokens")
						and chunk.usage._cache_read_input_tokens > 0
					):
						usage_dict[
							"cache_read_input_tokens"
						] = chunk.usage._cache_read_input_tokens
					merged_chunk["usage"] = usage_dict

					self.chunk_queue.append(merged_chunk)
					self.queued_usage_chunk = True
					self.holding_stop_reason_chunk = None
					return self.chunk_queue.popleft()

				if not self.queued_usage_chunk:
					if should_start_new_block and not self.sent_content_block_finish:
						_queue_content_block_switch(self, processed_chunk)
						return self.chunk_queue.popleft()

					if (
						processed_chunk["type"] == "message_delta"
						and self.sent_content_block_start is True
						and self.sent_content_block_finish is False
					):
						self.chunk_queue.append(
							{
								"type": "content_block_stop",
								"index": self.current_content_block_index,
							}
						)
						self.sent_content_block_finish = True
						if (
							processed_chunk.get("delta", {}).get("stop_reason")
							is not None
						):
							self.holding_stop_reason_chunk = processed_chunk
						else:
							self.chunk_queue.append(processed_chunk)
						return self.chunk_queue.popleft()
					elif self.holding_chunk is not None:
						self.chunk_queue.append(self.holding_chunk)
						self.chunk_queue.append(processed_chunk)
						self.holding_chunk = None
						return self.chunk_queue.popleft()
					else:
						self.chunk_queue.append(processed_chunk)
						return self.chunk_queue.popleft()

			if not self.queued_usage_chunk:
				if self.holding_stop_reason_chunk is not None:
					self.chunk_queue.append(self.holding_stop_reason_chunk)
					self.holding_stop_reason_chunk = None

				if self.holding_chunk is not None:
					self.chunk_queue.append(self.holding_chunk)
					self.holding_chunk = None

			if not self.sent_last_message:
				self.sent_last_message = True
				self.chunk_queue.append({"type": "message_stop"})

			if self.chunk_queue:
				return self.chunk_queue.popleft()

			raise StopIteration
		except StopIteration:
			if self.chunk_queue:
				return self.chunk_queue.popleft()
			if self.holding_stop_reason_chunk is not None:
				return self.holding_stop_reason_chunk
			if not self.sent_last_message:
				self.sent_last_message = True
				return {"type": "message_stop"}
			raise StopAsyncIteration

	_original_should_start_new_content_block = AnthropicStreamWrapper._should_start_new_content_block

	def _patched_should_start_new_content_block(self, chunk) -> bool:
		if not _has_actual_content(chunk):
			return False
		return _original_should_start_new_content_block(self, chunk)

	AnthropicStreamWrapper._should_start_new_content_block = _patched_should_start_new_content_block
	AnthropicStreamWrapper.__next__ = _patched_anthropic_stream_next
	AnthropicStreamWrapper.__anext__ = _patched_anthropic_stream_anext
	logger.info("Patched Anthropic streaming adapter to preserve tool input deltas.")
except Exception as e:
	logger.error(f"Failed to patch Anthropic streaming adapter: {e}", exc_info=True)

# 4c. Non-Anthropic reasoning models (kimi-for-coding, zai/glm) emit Anthropic
# `thinking` blocks but never a closing `signature_delta`, so the block's
# signature stays "". Claude Code v2.1.100+ rejects that with
# "Content block is not a text block". Inject a synthetic signature_delta before
# content_block_stop when a thinking block has not received one. The signature
# is opaque to Claude Code and the next turn is routed back through LiteLLM to
# the non-Anthropic provider, which ignores it, so a placeholder is safe.
try:
	from litellm.llms.anthropic.experimental_pass_through.adapters.streaming_iterator import (
		AnthropicStreamWrapper as _ASW,
	)

	_prev_asw_anext = _ASW.__anext__
	_prev_asw_next = _ASW.__next__
	_SIG_SENT_ATTR = "_ccproxy_sig_sent"
	_BLOCK_TYPES_ATTR = "_ccproxy_block_types"
	_PENDING_ATTR = "_ccproxy_sig_pending"
	_SYNTH_SIG = "ccproxy-synthetic-thinking-signature"

	def _asw_should_convert_thinking_to_text(self):
		model = str(getattr(self, "model", "")).lower()
		routed = str(getattr(self, "routed_model", "")).lower()
		custom_provider = str(getattr(self, "custom_llm_provider", "")).lower()

		if any(p in routed for p in ("minimax", "deepseek", "qwen", "glm", "mistral", "scaleway", "bielik")):
			return True
		if any(p in model for p in ("minimax", "deepseek", "qwen", "glm", "mistral", "scaleway", "bielik")):
			return True
		if not model.startswith("claude-") or (custom_provider and custom_provider != "anthropic"):
			return True
		if routed and not (routed.startswith("claude-") or routed.startswith("anthropic/")):
			return True
		return False

	def _asw_convert_thinking_to_text(self, chunk):
		"""Non-Claude providers emit reasoning as Anthropic thinking blocks, but
		Claude Code v2.1.205+ rejects them without valid Anthropic signatures.
		Format them as styled Markdown blockquotes so they render as distinct dim/italicized reasoning in Claude Code."""
		if not isinstance(chunk, dict):
			return chunk

		should_convert = _asw_should_convert_thinking_to_text(self)

		ctype = chunk.get("type")
		if ctype == "content_block_start":
			cb = chunk.get("content_block") or {}
			if should_convert and cb.get("type") == "thinking":
				cb["type"] = "text"
				thinking_txt = cb.get("thinking", "")
				if thinking_txt:
					lines = thinking_txt.strip().splitlines()
					cb["text"] = "> **Reasoning:**\n" + "\n".join(f"> *{line}*" if line.strip() else ">" for line in lines) + "\n\n"
				else:
					cb["text"] = ""
				cb.pop("thinking", None)
				cb.pop("signature", None)
				return chunk
		elif ctype == "content_block_delta":
			delta = chunk.get("delta") or {}
			if should_convert and delta.get("type") == "thinking_delta":
				delta["type"] = "text_delta"
				text_val = delta.get("thinking", "")
				if isinstance(text_val, str):
					text_val = text_val.replace("<think>", "").replace("</think>", "")
					if "\n" in text_val:
						text_val = text_val.replace("\n", "\n> *")
				delta["text"] = text_val
				delta.pop("thinking", None)
				if not delta.get("text") and text_val == "":
					return None
				return chunk
			elif should_convert and delta.get("type") == "signature_delta":
				# Non-Claude signatures are meaningless to Claude Code; drop them.
				return None
			elif delta.get("type") == "text_delta":
				text_val = delta.get("text", "")
				if isinstance(text_val, str) and ("<think>" in text_val or "</think>" in text_val):
					text_val = text_val.replace("<think>", "").replace("</think>", "")
					delta["text"] = text_val
					if not text_val:
						return None
				return chunk
		return chunk

	def _asw_maybe_inject_signature(self, chunk):
		if not isinstance(chunk, dict):
			return None
		ctype = chunk.get("type")
		types = getattr(self, _BLOCK_TYPES_ATTR, None)
		if types is None:
			types = {}
			setattr(self, _BLOCK_TYPES_ATTR, types)
		sigs = getattr(self, _SIG_SENT_ATTR, None)
		if sigs is None:
			sigs = set()
			setattr(self, _SIG_SENT_ATTR, sigs)
		if ctype == "content_block_start":
			idx = chunk.get("index", 0)
			cb = chunk.get("content_block") or {}
			types[idx] = cb.get("type")
			return None
		if (
			ctype == "content_block_delta"
			and isinstance(chunk.get("delta"), dict)
			and chunk["delta"].get("type") == "signature_delta"
		):
			sigs.add(chunk.get("index", getattr(self, "current_content_block_index", 0)))
			return None
		if ctype == "content_block_stop":
			idx = chunk.get("index", getattr(self, "current_content_block_index", 0))
			if types.get(idx) == "thinking" and idx not in sigs:
				sigs.add(idx)
				return {
					"type": "content_block_delta",
					"index": idx,
					"delta": {"type": "signature_delta", "signature": _SYNTH_SIG},
				}
		return None

	_STOPPED_BLOCKS_ATTR = "_ccproxy_stopped_blocks"
	_MM_STREAM_BUF_ATTR = "_ccproxy_mm_stream_buf"
	_MM_IS_BUFFERING_ATTR = "_ccproxy_mm_is_buffering"

	def _asw_process_minimax_streaming_tools(self, chunk):
		if not isinstance(chunk, dict):
			return [chunk]

		ctype = chunk.get("type")
		is_buffering = getattr(self, _MM_IS_BUFFERING_ATTR, False)

		if ctype == "content_block_delta":
			delta = chunk.get("delta") or {}
			if delta.get("type") == "text_delta":
				text = delta.get("text", "")
				if is_buffering or any(k in text for k in ("<tool_call>", "<minimax:tool_call>", "]<]minimax[>[", "<invoke")):
					buf = getattr(self, _MM_STREAM_BUF_ATTR, "") + text
					setattr(self, _MM_STREAM_BUF_ATTR, buf)
					setattr(self, _MM_IS_BUFFERING_ATTR, True)
					if any(k in buf for k in ("</tool_call>", "</minimax:tool_call>", "</invoke>")):
						setattr(self, _MM_IS_BUFFERING_ATTR, False)
						setattr(self, _MM_STREAM_BUF_ATTR, "")
						cleaned_text, tool_blocks = _parse_minimax_tool_call_text(buf)
						events = []
						if cleaned_text:
							events.append({
								"type": "content_block_delta",
								"index": 0,
								"delta": {"type": "text_delta", "text": cleaned_text}
							})
						events.append({"type": "content_block_stop", "index": 0})
						for tool_idx, tb in enumerate(tool_blocks, start=1):
							events.append({
								"type": "content_block_start",
								"index": tool_idx,
								"content_block": {
									"type": "tool_use",
									"id": tb["id"],
									"name": tb["name"],
									"input": {}
								}
							})
							events.append({
								"type": "content_block_delta",
								"index": tool_idx,
								"delta": {
									"type": "input_json_delta",
									"partial_json": json.dumps(tb["input"])
								}
							})
							events.append({"type": "content_block_stop", "index": tool_idx})
						events.append({
							"type": "message_delta",
							"delta": {"stop_reason": "tool_use"}
						})
						return events
					return []
		elif ctype in ("message_delta", "message_stop"):
			if is_buffering:
				buf = getattr(self, _MM_STREAM_BUF_ATTR, "")
				setattr(self, _MM_IS_BUFFERING_ATTR, False)
				setattr(self, _MM_STREAM_BUF_ATTR, "")
				cleaned_text, tool_blocks = _parse_minimax_tool_call_text(buf)
				events = []
				if cleaned_text:
					events.append({
						"type": "content_block_delta",
						"index": 0,
						"delta": {"type": "text_delta", "text": cleaned_text}
					})
				events.append({"type": "content_block_stop", "index": 0})
				for tool_idx, tb in enumerate(tool_blocks, start=1):
					events.append({
						"type": "content_block_start",
						"index": tool_idx,
						"content_block": {
							"type": "tool_use",
							"id": tb["id"],
							"name": tb["name"],
							"input": {}
						}
					})
					events.append({
						"type": "content_block_delta",
						"index": tool_idx,
						"delta": {
							"type": "input_json_delta",
							"partial_json": json.dumps(tb["input"])
						}
					})
					events.append({"type": "content_block_stop", "index": tool_idx})
				events.append({
					"type": "message_delta",
					"delta": {"stop_reason": "tool_use"}
				})
				events.append(chunk)
				return events

		return [chunk]

	def _asw_filter_stopped_deltas(self, chunk):
		if not isinstance(chunk, dict):
			return chunk
		ctype = chunk.get("type")
		stopped = getattr(self, _STOPPED_BLOCKS_ATTR, None)
		if stopped is None:
			stopped = set()
			setattr(self, _STOPPED_BLOCKS_ATTR, stopped)

		if ctype == "content_block_start":
			idx = chunk.get("index", 0)
			stopped.discard(idx)
			return chunk
		elif ctype == "content_block_stop":
			idx = chunk.get("index", 0)
			stopped.add(idx)
			return chunk
		elif ctype == "content_block_delta":
			idx = chunk.get("index", 0)
			if idx in stopped:
				return None
			return chunk
		return chunk

	async def _asw_wrapped_anext(self):
		pending = getattr(self, _PENDING_ATTR, None)
		if pending:
			return pending.pop(0)
		while True:
			chunk = await _prev_asw_anext(self)
			chunk = _asw_convert_thinking_to_text(self, chunk)
			chunk = _asw_filter_stopped_deltas(self, chunk)
			if chunk is None:
				continue
			events = _asw_process_minimax_streaming_tools(self, chunk)
			if not events:
				continue
			first_event = events.pop(0)
			if events:
				setattr(self, _PENDING_ATTR, events)
			inj = _asw_maybe_inject_signature(self, first_event)
			if inj is not None:
				p = getattr(self, _PENDING_ATTR, None) or []
				p.insert(0, first_event)
				setattr(self, _PENDING_ATTR, p)
				return inj
			return first_event

	def _asw_wrapped_next(self):
		pending = getattr(self, _PENDING_ATTR, None)
		if pending:
			return pending.pop(0)
		while True:
			chunk = _prev_asw_next(self)
			chunk = _asw_convert_thinking_to_text(self, chunk)
			chunk = _asw_filter_stopped_deltas(self, chunk)
			if chunk is None:
				continue
			events = _asw_process_minimax_streaming_tools(self, chunk)
			if not events:
				continue
			first_event = events.pop(0)
			if events:
				setattr(self, _PENDING_ATTR, events)
			inj = _asw_maybe_inject_signature(self, first_event)
			if inj is not None:
				p = getattr(self, _PENDING_ATTR, None) or []
				p.insert(0, first_event)
				setattr(self, _PENDING_ATTR, p)
				return inj
			return first_event

	_ASW.__anext__ = _asw_wrapped_anext
	_ASW.__next__ = _asw_wrapped_next
	logger.info("Patched AnthropicStreamWrapper: thinking->text, MiniMax tool calls, filter stopped deltas, synthetic signature.")
except Exception as e:
	logger.error(f"Failed to patch AnthropicStreamWrapper thinking blocks: {e}", exc_info=True)

# LiteLLM's Responses stream adapter can receive a `response.completed` event
# whose final response omits `output`, even though the stream already emitted a
# `function_call` item. Track the Anthropic block that was emitted so Claude
# Code receives the correct tool-use stop reason instead of `end_turn`.
try:
	from litellm.llms.anthropic.experimental_pass_through.responses_adapters.streaming_iterator import (
		AnthropicResponsesStreamWrapper,
	)

	def _maybe_rewrite_tool_use_stop_reason(self, chunk):
		if isinstance(chunk, dict):
			if (
				chunk.get("type") == "content_block_start"
				and isinstance(chunk.get("content_block"), dict)
				and chunk["content_block"].get("type") == "tool_use"
			):
				setattr(self, "_ccproxy_seen_tool_use", True)
			elif (
				chunk.get("type") == "message_delta"
				and getattr(self, "_ccproxy_seen_tool_use", False)
				and isinstance(chunk.get("delta"), dict)
				and chunk["delta"].get("stop_reason") == "end_turn"
			):
				chunk["delta"]["stop_reason"] = "tool_use"
		return chunk

	async def _patched_anthropic_responses_anext(self):
		if self._chunk_queue:
			return _maybe_rewrite_tool_use_stop_reason(self, self._chunk_queue.popleft())

		try:
			if hasattr(self.responses_stream, "__aiter__"):
				async for event in self.responses_stream:
					self._process_event(event)
					if self._chunk_queue:
						return _maybe_rewrite_tool_use_stop_reason(
							self, self._chunk_queue.popleft()
						)
			else:
				while True:
					event = next(self.responses_stream)
					self._process_event(event)
					if self._chunk_queue:
						return _maybe_rewrite_tool_use_stop_reason(
							self, self._chunk_queue.popleft()
						)
		except (StopAsyncIteration, StopIteration):
			pass
		except Exception as e:
			verbose_logger.error(
				f"AnthropicResponsesStreamWrapper error: {e}\n{traceback.format_exc()}"
			)

		if not self._sent_message_start:
			self._sent_message_start = True
			self._chunk_queue.append(self._make_message_start())

		if self._chunk_queue:
			return _maybe_rewrite_tool_use_stop_reason(self, self._chunk_queue.popleft())

		raise StopAsyncIteration

	AnthropicResponsesStreamWrapper.__anext__ = _patched_anthropic_responses_anext
	logger.info("Patched Responses Anthropic stream stop reason for omitted function-call output.")
except Exception as e:
	logger.error(f"Failed to patch Responses Anthropic stream stop reason: {e}", exc_info=True)

# Native chatgpt on LiteLLM >=1.91: skip the legacy SSE output-reconstruction
# patch (#5) and let the provider's own transform_response_api_response run.
# The ChatGPT provider emits a Responses stream; LiteLLM's native Anthropic
# Responses adapter translates it to Messages SSE and preserves function calls.
_NATIVE_CHATGPT_RESPONSES = True

# 5. ChatGPT's Codex backend currently emits completed Responses API output
# through `response.output_item.done` stream events, while the final
# `response.completed.response` object may omit `output`. LiteLLM
# expects final `output`, so reconstruct it from the stream items before
# handing the response to the normal transformer.
try:
	from litellm.llms.chatgpt.responses.transformation import ChatGPTResponsesAPIConfig
	from litellm.types.llms.openai import ResponsesAPIResponse, ResponsesAPIStreamEvents
	from litellm.utils import CustomStreamWrapper
	from litellm.litellm_core_utils.llm_response_utils.convert_dict_to_response import (
		_safe_convert_created_field,
	)
	from litellm.litellm_core_utils.core_helpers import process_response_headers
	from litellm.llms.openai.common_utils import OpenAIError

	_original_chatgpt_transform_response_api_response = (
		ChatGPTResponsesAPIConfig.transform_response_api_response
	)

	def _patched_chatgpt_transform_response_api_response(
		self,
		model,
		raw_response,
		logging_obj,
	):
		content_type = (raw_response.headers or {}).get("content-type", "")
		body_text = raw_response.text or ""
		looks_like_sse = (
			"text/event-stream" in content_type.lower()
			or body_text.lstrip().startswith(("event:", "data:"))
			or "\nevent:" in body_text
			or "\ndata:" in body_text
		)
		if not looks_like_sse:
			return _original_chatgpt_transform_response_api_response(
				self, model, raw_response, logging_obj
			)

		logging_obj.post_call(
			original_response=raw_response.text,
			additional_args={"complete_input_dict": {}},
		)

		output_items = []
		completed_response = None
		error_message = None
		for chunk in body_text.splitlines():
			stripped_chunk = CustomStreamWrapper._strip_sse_data_from_chunk(chunk)
			if not stripped_chunk:
				continue
			stripped_chunk = stripped_chunk.strip()
			if not stripped_chunk or stripped_chunk == "[DONE]":
				continue
			try:
				parsed_chunk = json.loads(stripped_chunk)
			except json.JSONDecodeError:
				continue
			if not isinstance(parsed_chunk, dict):
				continue

			event_type = parsed_chunk.get("type")
			if event_type == ResponsesAPIStreamEvents.OUTPUT_ITEM_DONE:
				item = parsed_chunk.get("item")
				if isinstance(item, dict):
					output_items.append(item)
				continue

			if event_type == ResponsesAPIStreamEvents.RESPONSE_COMPLETED:
				response_payload = parsed_chunk.get("response")
				if isinstance(response_payload, dict):
					response_payload = dict(response_payload)
					if not response_payload.get("output"):
						response_payload["output"] = output_items
					if "created_at" in response_payload:
						response_payload["created_at"] = _safe_convert_created_field(
							response_payload["created_at"]
						)
					try:
						completed_response = ResponsesAPIResponse(**response_payload)
					except Exception:
						completed_response = ResponsesAPIResponse.model_construct(
							**response_payload
						)
				break

			if event_type in (
				ResponsesAPIStreamEvents.RESPONSE_FAILED,
				ResponsesAPIStreamEvents.ERROR,
			):
				error_obj = parsed_chunk.get("error") or (
					parsed_chunk.get("response") or {}
				).get("error")
				if error_obj is not None:
					if isinstance(error_obj, dict):
						error_message = error_obj.get("message") or str(error_obj)
					else:
						error_message = str(error_obj)

		if completed_response is None:
			raise OpenAIError(
				message=error_message or raw_response.text,
				status_code=raw_response.status_code,
			)

		raw_headers = dict(raw_response.headers)
		processed_headers = process_response_headers(raw_headers)
		if not hasattr(completed_response, "_hidden_params"):
			setattr(completed_response, "_hidden_params", {})
		completed_response._hidden_params["additional_headers"] = processed_headers
		completed_response._hidden_params["headers"] = raw_headers
		return completed_response

	ChatGPTResponsesAPIConfig.transform_response_api_response = (
		_patched_chatgpt_transform_response_api_response
	)
	logger.info("Patched ChatGPT Responses SSE output reconstruction.")
except Exception as e:
	logger.error(f"Failed to patch ChatGPT Responses handling: {e}", exc_info=True)

# 6. Patch LiteLLM's Responses request transformer so chat-completions-style
#    function tools are flattened into the Responses schema (`type`, `name`,
#    `parameters`, ...). ChatGPT's Responses backend rejects nested
#    `tool.function.name` payloads with `Missing required parameter: tools[0].name`.
try:
	from litellm.llms.openai.responses.transformation import (
		OpenAIResponsesAPIConfig,
	)

	_original_transform_responses_api_request = (
		OpenAIResponsesAPIConfig.transform_responses_api_request
	)

	def _normalize_chat_completion_tools_to_responses(tools):
		if not isinstance(tools, list):
			return tools

		normalized = []
		for tool in tools:
			if not isinstance(tool, dict):
				continue

			if tool.get("type") != "function":
				normalized.append(tool)
				continue

			function_tool = tool.get("function")
			if not isinstance(function_tool, dict):
				function_tool = {}

			name = function_tool.get("name") or tool.get("name")
			if not isinstance(name, str) or not name.strip():
				continue
			name = name.strip()

			flat_tool: dict[str, Any] = {
				"type": "function",
				"name": name,
			}

			parameters = function_tool.get("parameters") or tool.get("parameters")
			if isinstance(parameters, dict):
				flat_tool["parameters"] = parameters

			strict = function_tool.get("strict")
			if strict is None:
				strict = tool.get("strict")
			if strict is not None:
				flat_tool["strict"] = strict

			description = function_tool.get("description") or tool.get("description")
			if isinstance(description, str) and description.strip():
				flat_tool["description"] = description.strip()

			normalized.append(flat_tool)

		return normalized

	def _patched_transform_responses_api_request(
		self,
		model,
		input,
		response_api_optional_request_params,
		litellm_params,
		headers,
	):
		if isinstance(response_api_optional_request_params, dict):
			tools = response_api_optional_request_params.get("tools")
			if isinstance(tools, list):
				response_api_optional_request_params = dict(response_api_optional_request_params)
				response_api_optional_request_params["tools"] = (
					_normalize_chat_completion_tools_to_responses(tools)
				)
		return _original_transform_responses_api_request(
			self,
			model,
			input,
			response_api_optional_request_params,
			litellm_params,
			headers,
		)

	if not _NATIVE_CHATGPT_RESPONSES:
		OpenAIResponsesAPIConfig.transform_responses_api_request = (
			_patched_transform_responses_api_request
		)
		logger.info("Patched LiteLLM Responses request transformer for chat-style function tools.")
	else:
		logger.info("Native chatgpt responses: Patch #6 (tool flattening) skipped.")
except Exception as e:
	logger.error(f"Failed to patch LiteLLM Responses request transformation: {e}", exc_info=True)

# 6c. ChatGPT's Codex backend requires Responses-style FLAT function tools
#     ({"type":"function","name":...,"parameters":...}). Native LiteLLM leaves
#     chat-style nested tools ({"type":"function","function":{"name":...}})
#     intact, so the Codex backend cannot read the schema and the model emits
#     empty tool arguments {}. Flatten tools on ChatGPTResponsesAPIConfig.
try:
	from litellm.llms.chatgpt.responses.transformation import (
		ChatGPTResponsesAPIConfig as _ChatGPTRespCfg,
	)

	_orig_chatgpt_transform_responses_api_request = (
		_ChatGPTRespCfg.transform_responses_api_request
	)

	def _chatgpt_flat_tools_transform_responses_api_request(
		self, model, input, response_api_optional_request_params, litellm_params, headers, **kwargs
	):
		request = _orig_chatgpt_transform_responses_api_request(
			self, model, input, response_api_optional_request_params, litellm_params, headers, **kwargs
		)
		if isinstance(request, dict) and isinstance(request.get("tools"), list):
			request["tools"] = _normalize_chat_completion_tools_to_responses(request["tools"])
		# Codex currently requires stream=true on the upstream Responses API even
		# when the caller asked LiteLLM for a completed non-stream response.
		# Preserve the caller's stream semantics inside LiteLLM, but always keep
		# the upstream ChatGPT request streaming so the bridge can reconstruct a
		# completed response from the SSE transcript when needed.
		if isinstance(request, dict):
			request["stream"] = True
		return request

	_ChatGPTRespCfg.transform_responses_api_request = (
		_chatgpt_flat_tools_transform_responses_api_request
	)
	logger.info("Patched ChatGPTResponsesAPIConfig to flatten tools for Codex backend.")
except Exception as e:
	logger.error(f"Failed to patch ChatGPT tools flattening: {e}", exc_info=True)

# 6b. Route OpenAI/Azure `/v1/messages` requests through the chat-completions
#     adapter. LiteLLM's Responses adapter is useful for plain OpenAI models,
#     but fallback chains that include OpenAI-compatible backends such as
#     DeepSeek V4 Pro need the standard chat `/chat/completions` transport;
#     those providers do not implement `/responses`, and Claude Code tool loops
#     also need the chat `tools`/`tool_choice` contract.
try:
	from litellm.llms.anthropic.experimental_pass_through.responses_adapters.handler import (
		_ADAPTER,
		_build_responses_kwargs,
		AnthropicResponsesStreamWrapper,
		LiteLLMMessagesToResponsesAPIHandler,
	)
	from litellm.responses.streaming_iterator import BaseResponsesAPIStreamingIterator
	from litellm.types.llms.openai import ResponsesAPIResponse

	_original_responses_anthropic_messages_handler = (
		LiteLLMMessagesToResponsesAPIHandler.async_anthropic_messages_handler
	)
	_original_sync_responses_anthropic_messages_handler = (
		LiteLLMMessagesToResponsesAPIHandler.anthropic_messages_handler
	)

	def _coerce_completed_responses_result(result):
		if isinstance(result, ResponsesAPIResponse):
			return result
		if isinstance(result, BaseResponsesAPIStreamingIterator):
			provider_config = getattr(result, "responses_api_provider_config", None)
			raw_response = getattr(result, "response", None)
			logging_obj = getattr(result, "logging_obj", None)
			if provider_config is not None and raw_response is not None and logging_obj is not None:
				try:
					reconstructed = provider_config.transform_response_api_response(
						model=getattr(result, "model", ""),
						raw_response=raw_response,
						logging_obj=logging_obj,
					)
					if isinstance(reconstructed, ResponsesAPIResponse):
						return reconstructed
				except Exception:
					pass
			completed = result._get_completed_response_object()
			if isinstance(completed, ResponsesAPIResponse):
				return completed
		raise ValueError(f"Expected ResponsesAPIResponse, got {type(result)}")

	def _maybe_backfill_responses_output(
		completed_response: ResponsesAPIResponse,
		output_items: list,
	) -> ResponsesAPIResponse:
		if output_items and not getattr(completed_response, "output", None):
			try:
				completed_response.output = output_items
			except Exception:
				pass
		return completed_response

	def _maybe_collect_output_item_done(chunk, output_items: list) -> None:
		chunk_type = getattr(chunk, "type", None)
		if chunk_type is None and isinstance(chunk, dict):
			chunk_type = chunk.get("type")
		if chunk_type != "response.output_item.done":
			return
		item = getattr(chunk, "item", None)
		if item is None and isinstance(chunk, dict):
			item = chunk.get("item")
		if item is not None:
			if hasattr(item, "model_dump"):
				try:
					item = item.model_dump()
				except Exception:
					pass
			output_items.append(item)

	def _patched_sync_responses_anthropic_messages_handler(
		max_tokens,
		messages,
		model,
		context_management=None,
		metadata=None,
		output_config=None,
		stop_sequences=None,
		stream=False,
		system=None,
		temperature=None,
		thinking=None,
		tool_choice=None,
		tools=None,
		top_k=None,
		top_p=None,
		output_format=None,
		_is_async=False,
		**kwargs,
	):
		if _CHATGPT_PATCHES_ENABLED and _is_chatgpt_model(
			model, kwargs.get("custom_llm_provider"), metadata
		):
			responses_kwargs = _build_responses_kwargs(
				max_tokens=max_tokens,
				messages=messages,
				model=model,
				context_management=context_management,
				metadata=metadata,
				output_config=output_config,
				stop_sequences=stop_sequences,
				stream=stream,
				system=system,
				temperature=temperature,
				thinking=thinking,
				tool_choice=tool_choice,
				tools=tools,
				top_k=top_k,
				top_p=top_p,
				output_format=output_format,
				extra_kwargs=kwargs,
			)
			cache_kwargs = responses_kwargs.get("cache")
			if not isinstance(cache_kwargs, dict):
				cache_kwargs = {}
			cache_kwargs["no-cache"] = True
			responses_kwargs["cache"] = cache_kwargs
			result = litellm.responses(**responses_kwargs)
			if stream:
				wrapper = AnthropicResponsesStreamWrapper(responses_stream=result, model=model)
				return wrapper.async_anthropic_sse_wrapper()
			output_items = []
			if isinstance(result, BaseResponsesAPIStreamingIterator):
				for chunk in result:
					_maybe_collect_output_item_done(chunk, output_items)
			completed_response = _coerce_completed_responses_result(result)
			completed_response = _maybe_backfill_responses_output(
				completed_response,
				output_items,
			)
			return _ADAPTER.translate_response(completed_response)
		return LiteLLMMessagesToCompletionTransformationHandler.anthropic_messages_handler(
			max_tokens=max_tokens,
			messages=messages,
			model=model,
			context_management=context_management,
			metadata=metadata,
			output_config=output_config,
			stop_sequences=stop_sequences,
			stream=stream,
			system=system,
			temperature=temperature,
			thinking=thinking,
			tool_choice=tool_choice,
			tools=tools,
			top_k=top_k,
			top_p=top_p,
			output_format=output_format,
			_is_async=_is_async,
			**kwargs,
		)

	async def _patched_responses_anthropic_messages_handler(
		max_tokens,
		messages,
		model,
		context_management=None,
		metadata=None,
		output_config=None,
		stop_sequences=None,
		stream=False,
		system=None,
		temperature=None,
		thinking=None,
		tool_choice=None,
		tools=None,
		top_k=None,
		top_p=None,
		output_format=None,
		**kwargs,
	):
		if _CHATGPT_PATCHES_ENABLED and _is_chatgpt_model(
			model, kwargs.get("custom_llm_provider"), metadata
		):
			responses_kwargs = _build_responses_kwargs(
				max_tokens=max_tokens,
				messages=messages,
				model=model,
				context_management=context_management,
				metadata=metadata,
				output_config=output_config,
				stop_sequences=stop_sequences,
				stream=stream,
				system=system,
				temperature=temperature,
				thinking=thinking,
				tool_choice=tool_choice,
				tools=tools,
				top_k=top_k,
				top_p=top_p,
				output_format=output_format,
				extra_kwargs=kwargs,
			)
			cache_kwargs = responses_kwargs.get("cache")
			if not isinstance(cache_kwargs, dict):
				cache_kwargs = {}
			cache_kwargs["no-cache"] = True
			responses_kwargs["cache"] = cache_kwargs
			result = await litellm.aresponses(**responses_kwargs)
			if stream:
				wrapper = AnthropicResponsesStreamWrapper(responses_stream=result, model=model)
				return wrapper.async_anthropic_sse_wrapper()
			output_items = []
			if isinstance(result, BaseResponsesAPIStreamingIterator):
				async for chunk in result:
					_maybe_collect_output_item_done(chunk, output_items)
			completed_response = _coerce_completed_responses_result(result)
			completed_response = _maybe_backfill_responses_output(
				completed_response,
				output_items,
			)
			return _ADAPTER.translate_response(completed_response)
		return await LiteLLMMessagesToCompletionTransformationHandler.async_anthropic_messages_handler(
			max_tokens=max_tokens,
			messages=messages,
			model=model,
			context_management=context_management,
			metadata=metadata,
			output_config=output_config,
			stop_sequences=stop_sequences,
			stream=stream,
			system=system,
			temperature=temperature,
			thinking=thinking,
			tool_choice=tool_choice,
			tools=tools,
			top_k=top_k,
			top_p=top_p,
			output_format=output_format,
			**kwargs,
		)

	LiteLLMMessagesToResponsesAPIHandler.async_anthropic_messages_handler = (
		staticmethod(_patched_responses_anthropic_messages_handler)
	)
	LiteLLMMessagesToResponsesAPIHandler.anthropic_messages_handler = (
		staticmethod(_patched_sync_responses_anthropic_messages_handler)
	)
	logger.info(
		"Patched Responses-backed /v1/messages requests with provider-aware chat fallback."
	)
except Exception as e:
	logger.error(f"Failed to patch Responses-backed /v1/messages tool routing: {e}", exc_info=True)

# 7. Avoid noisy non-blocking success-logging crashes for Responses streaming
# events whose `response` payload is already a dict.
try:
	from litellm.litellm_core_utils.litellm_logging import Logging

	_original_get_assembled_streaming_response = Logging._get_assembled_streaming_response

	def _patched_get_assembled_streaming_response(self, *args, **kwargs):
		try:
			return _original_get_assembled_streaming_response(self, *args, **kwargs)
		except AttributeError as exc:
			if "'dict' object has no attribute 'usage'" in str(exc):
				return None
			raise

	Logging._get_assembled_streaming_response = _patched_get_assembled_streaming_response
	logger.info("Patched LiteLLM streaming success logging for dict Responses payloads.")
except Exception as e:
	logger.error(f"Failed to patch LiteLLM streaming success logging: {e}", exc_info=True)


# 8. Fallback instrumentation — log at WARNING when fallback lookup fails
#    or when the router fallback state appears inconsistent.
try:
	from litellm.router import Router
	from litellm._logging import verbose_router_logger
	from litellm.router_utils.fallback_event_handlers import (
		get_fallback_model_group as _orig_get_fallback_model_group,
	)

	def _instrumented_get_fallback_model_group(fallbacks, model_group):
		result, idx = _orig_get_fallback_model_group(fallbacks, model_group)
		if result is None and fallbacks is not None:
			keys = []
			for item in fallbacks:
				if isinstance(item, dict):
					keys.extend(list(item.keys()))
			verbose_router_logger.warning(
				f"[FALLBACK_GUARD] get_fallback_model_group returned None for "
				f"model_group={model_group}. Available keys in fallbacks: {keys}"
			)
		return result, idx

	# Patch the module-level function used by router.py
	import litellm.router_utils.fallback_event_handlers as _fallback_mod

	_fallback_mod.get_fallback_model_group = _instrumented_get_fallback_model_group

	_original_common_utils = Router.async_function_with_fallbacks_common_utils

	async def _instrumented_async_function_with_fallbacks_common_utils(
		self, e, disable_fallbacks, fallbacks, context_window_fallbacks,
		content_policy_fallbacks, model_group, args, kwargs, **extra
	):
		# Detect router state drift: fallbacks param is None but router has them
		if fallbacks is None and getattr(self, "fallbacks", None) is not None:
			verbose_router_logger.warning(
				f"[FALLBACK_GUARD] fallbacks param is None but self.fallbacks has "
				f"{len(self.fallbacks)} entries for model_group={model_group}. "
				f"Restoring router fallback config for this request."
			)
			if not disable_fallbacks:
				fallbacks = self.fallbacks
		if (
			context_window_fallbacks is None
			and getattr(self, "context_window_fallbacks", None) is not None
			and not disable_fallbacks
		):
			context_window_fallbacks = self.context_window_fallbacks
		if (
			content_policy_fallbacks is None
			and getattr(self, "content_policy_fallbacks", None) is not None
			and not disable_fallbacks
		):
			content_policy_fallbacks = self.content_policy_fallbacks
		return await _original_common_utils(
			self, e, disable_fallbacks, fallbacks, context_window_fallbacks,
			content_policy_fallbacks, model_group, args, kwargs, **extra
		)

	Router.async_function_with_fallbacks_common_utils = _instrumented_async_function_with_fallbacks_common_utils
	logger.info("Instrumented fallback logic with WARNING-level guards.")
except Exception as e:
	logger.error(f"Failed to instrument fallback logic: {e}", exc_info=True)

# 9. Harden fallback recursion against upstream KeyError bugs.
#
#    a) run_async_fallback calls log_retry before ensuring metadata dicts exist.
#    b) async_function_with_retries destructively pops original_function / num_retries.
#       Although **kwargs creates a new dict per call in most paths, we harden the
#       functions so they never crash on missing keys.
try:
	from litellm.router_utils.fallback_event_handlers import (
		run_async_fallback as _orig_run_async_fallback,
	)
	from litellm.router import Router

	async def _patched_run_async_fallback(
		*args,
		litellm_router,
		fallback_model_group,
		original_model_group,
		original_exception,
		max_fallbacks,
		fallback_depth,
		**kwargs,
	):
		# Ensure metadata/litellm_metadata dicts exist before log_retry is called
		for key in ("metadata", "litellm_metadata"):
			if kwargs.get(key) is None:
				kwargs[key] = {}
		return await _orig_run_async_fallback(
			*args,
			litellm_router=litellm_router,
			fallback_model_group=fallback_model_group,
			original_model_group=original_model_group,
			original_exception=original_exception,
			max_fallbacks=max_fallbacks,
			fallback_depth=fallback_depth,
			**kwargs,
		)

	# Replace the module reference so router.py picks it up
	import litellm.router_utils.fallback_event_handlers as _fallback_mod2

	_fallback_mod2.run_async_fallback = _patched_run_async_fallback

	_original_retries = Router.async_function_with_retries

	async def _patched_async_function_with_retries(self, *args, **kwargs):
		# Defensive: never crash if original_function or num_retries are missing.
		# Use .pop() with a sentinel and re-inject if the caller omitted them,
		# so fallback recursion always has the keys it expects.
		_sentinel = object()
		orig_fn = kwargs.pop("original_function", _sentinel)
		if orig_fn is _sentinel:
			# Fallback recursion may have lost original_function; use a no-op
			async def _noop(*a, **kw):
				raise litellm.InternalServerError(
					model=kwargs.get("model", ""),
					llm_provider="",
					message="Missing original_function in fallback recursion",
				)
			kwargs["original_function"] = _noop
		else:
			kwargs["original_function"] = orig_fn

		num_retries = kwargs.pop("num_retries", _sentinel)
		if num_retries is _sentinel:
			kwargs["num_retries"] = self.num_retries
		else:
			kwargs["num_retries"] = num_retries

		return await _original_retries(self, *args, **kwargs)

	Router.async_function_with_retries = _patched_async_function_with_retries
	logger.info("Hardened fallback recursion against upstream KeyError bugs.")
except Exception as e:
	logger.error(f"Failed to harden fallback recursion: {e}", exc_info=True)

# 10. Preserve router stream_timeout for streaming generic API calls.
#
#     LiteLLM 1.82.x computes the stream timeout for router-managed streaming
#     calls, but stores it in kwargs["timeout"]. The Anthropic /v1/messages
#     adapter then forwards only "timeout" to litellm.acompletion(), which does
#     not enforce first-byte timeout. Keep the explicit stream_timeout too so
#     zero-first-byte streams can fail and route through fallbacks.
try:
	from litellm.router import Router

	_original_update_kwargs_with_deployment = Router._update_kwargs_with_deployment

	def _sanitize_messages_for_token_counter(messages):
		if not isinstance(messages, list):
			return messages

		def _sanitize_node(node):
			if isinstance(node, dict):
				if node.get("type") == "image":
					return {
						"type": "image_url",
						"image_url": {
							"url": "data:image/jpeg;base64,/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAP//////////////////////////////////////////////////////////////////////////////////////wgALCAABAAEBAREA/8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPxA="
						}
					}
				cleaned = {}
				for k, v in node.items():
					if k == "content" and isinstance(v, list):
						cleaned[k] = [_sanitize_node(sub) for sub in v]
					elif isinstance(v, (dict, list)):
						cleaned[k] = _sanitize_node(v)
					else:
						cleaned[k] = v
				return cleaned
			elif isinstance(node, list):
				return [_sanitize_node(item) for item in node]
			return node

		return [_sanitize_node(msg) for msg in messages]

	def _provider_output_cap_for_model(*model_names):
		"""Resolve a provider hard output ceiling from litellm.model_cost."""
		model_cost = getattr(litellm, "model_cost", {})
		if not isinstance(model_cost, dict):
			return None
		candidates = []
		seen = set()
		for raw_name in model_names:
			name = str(raw_name or "").strip()
			if not name:
				continue
			lower = name.lower()
			for candidate in (name, lower):
				if candidate and candidate not in seen:
					candidates.append(candidate)
					seen.add(candidate)
			if "/" in lower:
				stripped = lower.split("/", 1)[1]
				for candidate in (
					stripped,
					f"openai/{stripped}",
					f"zai/{stripped}",
					f"chatgpt/{stripped}",
					f"anthropic/{stripped}",
					f"moonshot/{stripped}",
					f"deepseek/{stripped}",
					f"mistral/{stripped}",
					f"minimax/{stripped}",
					f"qwencloud/{stripped}",
					f"qwencloud-payg/{stripped}",
					f"scaleway/{stripped}",
					f"cf/{stripped}",
					f"or/{stripped}",
				):
					if candidate not in seen:
						candidates.append(candidate)
						seen.add(candidate)
		for candidate in candidates:
			entry = model_cost.get(candidate)
			if not isinstance(entry, dict):
				continue
			max_out = entry.get("max_output_tokens")
			if isinstance(max_out, (int, float)) and max_out > 0:
				return int(max_out)
		return None

	def _patched_update_kwargs_with_deployment(
		self,
		deployment,
		kwargs,
		function_name=None,
	):
		_original_update_kwargs_with_deployment(
			self,
			deployment=deployment,
			kwargs=kwargs,
			function_name=function_name,
		)
		
		# Enforce deployment-level parameters (extra_headers/extra_body) to prevent shallow dict override bugs
		params = deployment.get("litellm_params", {}) if isinstance(deployment, dict) else {}
		for key in ("extra_headers", "extra_body"):
			dep_dict = params.get(key)
			if isinstance(dep_dict, dict) and dep_dict:
				existing = kwargs.get(key)
				if isinstance(existing, dict):
					kwargs[key] = {**existing, **dep_dict}
				else:
					kwargs[key] = dep_dict.copy()

		model_name = deployment.get("model_name") if isinstance(deployment, dict) else None
		if model_name and model_name.startswith("chatgpt/"):
			kwargs["disable_fallbacks"] = True

		# Enforce context window limits defensively and prevent negative max_tokens
		try:
			model_name = deployment.get("model_name")
			litellm_model = params.get("model")
			model_info = deployment.get("model_info", {})
			context_window = model_info.get("context_window")
			if not context_window and litellm_model:
				context_window = litellm.model_cost.get(litellm_model, {}).get("max_tokens")
			
			if not context_window:
				# Use sensible defaults based on the model name/litellm_model
				litellm_model_lower = str(litellm_model or "").lower()
				model_name_lower = str(model_name or "").lower()
				if "deepseek" in litellm_model_lower or "deepseek" in model_name_lower:
					context_window = 1000000
				elif "kimi" in litellm_model_lower or "kimi" in model_name_lower:
					context_window = 262144
				elif "scaleway" in litellm_model_lower or "scaleway" in model_name_lower:
					context_window = 200000
				elif "gemma" in litellm_model_lower or "gemma" in model_name_lower:
					context_window = 131072
				elif "bielik" in litellm_model_lower or "bielik" in model_name_lower:
					context_window = 16384
				elif "glm-5.2[1m]" in litellm_model_lower or "glm-5.2[1m]" in model_name_lower or "glm-5.2" in litellm_model_lower or "glm-5.2" in model_name_lower:
					context_window = 1000000
				elif "glm" in litellm_model_lower or "glm" in model_name_lower:
					context_window = 200000
				elif "qwen" in litellm_model_lower or "qwen" in model_name_lower:
					context_window = 32000
				elif "claude" in litellm_model_lower or "claude" in model_name_lower:
					context_window = 200000

			import sys
			print(
				f"[sitecustomize] Enforcing context window check for deployment={model_name}, "
				f"litellm_model={litellm_model}, context_window={context_window}",
				file=sys.stderr,
				flush=True,
			)

			if context_window:
				# Resolve messages by checking kwargs, then walking up the stack frames
				messages = kwargs.get("messages")
				if not messages:
					frame = sys._getframe(0)
					while frame:
						if "messages" in frame.f_locals:
							messages = frame.f_locals["messages"]
							break
						if "kwargs" in frame.f_locals and isinstance(frame.f_locals["kwargs"], dict) and "messages" in frame.f_locals["kwargs"]:
							messages = frame.f_locals["kwargs"]["messages"]
							break
						frame = frame.f_back
				
				print(f"[sitecustomize] Resolved messages from stack frame/kwargs: {messages is not None} (kwargs keys: {list(kwargs.keys()) if isinstance(kwargs, dict) else type(kwargs)})", file=sys.stderr, flush=True)

				if messages:
					# Move the initial system message out of messages[0] for Anthropic.
					# OpenAI-style providers tolerate system-role messages inside the
					# array; Anthropic rejects them outright and demands the prompt at
					# the top-level ``system`` parameter. Without this fix, every
					# Anthropic call returns 400 ``messages.0: use the top-level
					# 'system' parameter ...``.
					try:
						_is_anthropic = isinstance(litellm_model, str) and (
							litellm_model.startswith("anthropic/")
							or "/anthropic" in litellm_model
						)
						if (
							_is_anthropic
							and isinstance(messages, list)
							and messages
							and isinstance(messages[0], dict)
							and messages[0].get("role") == "system"
						):
							sys_msg = messages[0]
							output_config = sys_msg.get("output_config")
							if output_config is not None and kwargs.get("output_config") is None:
								kwargs["output_config"] = output_config

							content = sys_msg.get("content")
							current_system = kwargs.get("system")
							if isinstance(content, str):
								if isinstance(current_system, str):
									kwargs["system"] = (current_system + "\n\n" + content) if current_system else content
								elif isinstance(current_system, list):
									kwargs["system"] = current_system + [{"type": "text", "text": content}]
								elif current_system is None:
									kwargs["system"] = content
							elif isinstance(content, list):
								if isinstance(current_system, str):
									kwargs["system"] = (
										[{"type": "text", "text": current_system}] + content
										if current_system else content
									)
								elif isinstance(current_system, list):
									kwargs["system"] = current_system + content
								elif current_system is None:
									kwargs["system"] = content

							messages.pop(0)
							print("[sitecustomize] Normalized initial system message from messages[0] to top-level system", file=sys.stderr, flush=True)
					except Exception as norm_exc:
						print(f"[sitecustomize] Warning: system message normalization failed: {norm_exc}", file=sys.stderr, flush=True)

					try:
						sanitized_messages = _sanitize_messages_for_token_counter(messages)
						prompt_tokens = litellm.token_counter(model=litellm_model, messages=sanitized_messages)
					except Exception as tc_exc:
						print(f"[sitecustomize] Warning: token_counter failed for model={litellm_model}: {tc_exc}", file=sys.stderr, flush=True)
						try:
							print(f"[sitecustomize] Debug messages type: {type(messages)}, len: {len(messages) if isinstance(messages, list) else 'N/A'}", file=sys.stderr, flush=True)
							if messages and isinstance(messages, list):
								for idx, m in enumerate(messages[:3]):
									print(f"[sitecustomize] Debug msg[{idx}] type: {type(m)}, content type: {type(m.get('content')) if isinstance(m, dict) else 'N/A'}", file=sys.stderr, flush=True)
									if isinstance(m, dict) and isinstance(m.get('content'), list):
										print(f"[sitecustomize] Debug msg[{idx}] content items: {[type(item) for item in m.get('content')]}", file=sys.stderr, flush=True)
						except Exception as dbg_err:
							print(f"[sitecustomize] Debug print failed: {dbg_err}", file=sys.stderr, flush=True)
						# Fallback simple estimation: 1 token per 3 characters of text content
						try:
							char_count = 0
							for msg in messages:
								content = msg.get("content", "")
								if isinstance(content, str):
									char_count += len(content)
								elif isinstance(content, list):
									for item in content:
										if isinstance(item, dict):
											char_count += len(item.get("text", ""))
											if item.get("type") == "image":
												char_count += 4000
										elif isinstance(item, str):
											char_count += len(item)
							prompt_tokens = max(1, char_count // 3)
							print(f"[sitecustomize] Fallback prompt tokens estimated: {prompt_tokens}", file=sys.stderr, flush=True)
						except Exception as est_exc:
							print(f"[sitecustomize] Fallback estimation failed: {est_exc}", file=sys.stderr, flush=True)
							prompt_tokens = 0
					requested_max_tokens = kwargs.get("max_tokens")
					if requested_max_tokens is None:
						requested_max_tokens = params.get("max_tokens") or 4096
					
					# Coerce requested_max_tokens to integer if possible
					if not isinstance(requested_max_tokens, (int, float)):
						try:
							requested_max_tokens = int(requested_max_tokens)
						except (TypeError, ValueError):
							requested_max_tokens = 4096

					available_tokens = context_window - prompt_tokens
					print(
						f"[sitecustomize] Prompt tokens calculated: {prompt_tokens}, "
						f"requested max_tokens: {requested_max_tokens}, available tokens: {available_tokens}",
						file=sys.stderr,
						flush=True,
					)

					if available_tokens < 1:
						if _ENFORCE_CUSTOM_CONTEXT_CHECK:
							print(
								f"[sitecustomize] Prompt tokens ({prompt_tokens}) exceed context window ({context_window}) "
								f"for model {model_name}. Raising ContextWindowExceededError.",
								file=sys.stderr,
								flush=True,
							)
							raise litellm.ContextWindowExceededError(
								message=f"Prompt tokens ({prompt_tokens}) exceed model context window ({context_window}) for deployment {model_name}",
								model=model_name,
								llm_provider=""
							)
						# Rejection disabled: leave max_tokens to the clamp below or the backend.
						print(
							f"[sitecustomize] Prompt tokens ({prompt_tokens}) exceed declared context window ({context_window}) "
							f"but rejection disabled; will not raise.",
							file=sys.stderr,
							flush=True,
						)
						# Clamp max_tokens to the remaining budget. Only when there IS a
					# remaining budget — when the prompt already overflows and rejection
					# is disabled, leave it untouched so the backend (or configured
					# context_window_fallbacks) handles the overflow instead of forcing
					# a useless max_tokens=1 response.
					if available_tokens >= 1 and requested_max_tokens > available_tokens:
						target_val = min(available_tokens, params.get("max_tokens") or available_tokens)
						if target_val < 1:
							target_val = 1
						print(f"[sitecustomize] Adjusting max_tokens from {requested_max_tokens} to {target_val}", file=sys.stderr, flush=True)
						kwargs["max_tokens"] = target_val
					elif available_tokens < 1:
						# When prompt overflows declared context window and rejection is disabled,
						# force max_tokens to a valid positive default so upstream/LiteLLM
						# cannot compute negative max_tokens (-239344).
						safe_default = int(params.get("max_tokens") or 1024)
						print(f"[sitecustomize] Prompt overflow: overriding max_tokens to safe positive default {safe_default}", file=sys.stderr, flush=True)
						kwargs["max_tokens"] = safe_default
						if "max_completion_tokens" in kwargs and isinstance(kwargs["max_completion_tokens"], (int, float)) and kwargs["max_completion_tokens"] < 1:
							kwargs["max_completion_tokens"] = safe_default
		except litellm.ContextWindowExceededError:
			raise
		except Exception as e:
			print(f"[sitecustomize] Failed to check context window size: {e}", file=sys.stderr, flush=True)
			import traceback
			traceback.print_exc(file=sys.stderr)

		# Extra safety clamp to guarantee positive max_tokens and max_completion_tokens under all circumstances
		try:
			for token_field in ("max_completion_tokens", "max_tokens"):
				field_val = kwargs.get(token_field)
				if field_val is not None:
					try:
						val = int(field_val)
						if val < 1:
							target_val = int(params.get(token_field) or 1024)
							print(f"[sitecustomize] Safety-clamping invalid {token_field}={val} to {target_val}", file=sys.stderr, flush=True)
							kwargs[token_field] = target_val
						else:
							kwargs[token_field] = val
					except (TypeError, ValueError):
						pass
		except Exception:
			pass

		# Providers like GLM expose a large context window but still enforce a
		# smaller hard output ceiling. LiteLLM can translate Anthropic max_tokens
		# into max_completion_tokens after fallback selection, so cap both fields
		# here using the final deployment identity.
		try:
			provider_output_cap = _provider_output_cap_for_model(
				model_name,
				litellm_model,
				kwargs.get("model"),
			)
			for token_field in ("max_completion_tokens", "max_tokens"):
				value = kwargs.get(token_field)
				if (
					provider_output_cap is not None
					and isinstance(value, (int, float))
					and value > provider_output_cap
				):
					print(
						f"[sitecustomize] Applying provider output cap for deployment={model_name}, "
						f"litellm_model={litellm_model}: {token_field} {value} -> {provider_output_cap}",
						file=sys.stderr,
						flush=True,
					)
					kwargs[token_field] = provider_output_cap
		except Exception as cap_exc:
			print(f"[sitecustomize] Failed to apply provider output cap: {cap_exc}", file=sys.stderr, flush=True)

		if kwargs.get("stream") is not True or kwargs.get("stream_timeout") is not None:
			return
		stream_timeout = params.get("stream_timeout")
		if stream_timeout is None:
			stream_timeout = getattr(self, "stream_timeout", None)
		if stream_timeout is None:
			default_params = getattr(self, "default_litellm_params", {}) or {}
			stream_timeout = default_params.get("stream_timeout")
		if stream_timeout is not None:
			kwargs["stream_timeout"] = stream_timeout
			logger.debug(
				"Applied explicit stream_timeout=%s for streaming deployment %s",
				stream_timeout,
				deployment.get("model_name") if isinstance(deployment, dict) else None,
			)

	Router._update_kwargs_with_deployment = _patched_update_kwargs_with_deployment
	logger.info("Patched LiteLLM router to preserve stream_timeout for streaming calls.")
except Exception as e:
	logger.error(f"Failed to patch LiteLLM router stream_timeout handling: {e}", exc_info=True)

# 11. Passthrough interceptor: rewrite /anthropic/v1/messages -> /v1/messages
#    so that requests cannot accidentally bypass the router-backed endpoint
#    (and therefore bypass fallbacks) by using the passthrough path.
try:
	from litellm.proxy.proxy_server import app

	class _AnthropicPassthroughInterceptor:
		"""Pure ASGI middleware that rewrites /anthropic/v1/messages to /v1/messages."""
		def __init__(self, asgi_app):
			self.asgi_app = asgi_app

		async def __call__(self, scope, receive, send):
			if scope.get("type") == "http" and scope.get("path") == "/anthropic/v1/messages":
				scope = dict(scope)
				scope["path"] = "/v1/messages"
			await self.asgi_app(scope, receive, send)

	app.add_middleware(_AnthropicPassthroughInterceptor)
	logger.info("Installed passthrough interceptor: /anthropic/v1/messages -> /v1/messages")
except Exception as e:
	logger.error(f"Failed to install passthrough interceptor: {e}", exc_info=True)

# 12. PostgreSQL cannot store NUL characters in text/jsonb fields. Some provider
#    payloads can contain actual NUL bytes, escaped \u0000 sequences, or invalid
#    JSON text in spend log metadata, which makes LiteLLM's background spend-log
#    writer fail.
try:
	import re
	import httpx
	from litellm.proxy import utils as _proxy_utils

	_original_jsonify_object = _proxy_utils.PrismaClient.jsonify_object
	_pg_nul_escape_re = re.compile(r"\\u0000", re.IGNORECASE)
	_spend_log_json_fields = {
		"metadata",
		"messages",
		"proxy_server_request",
		"request_tags",
		"response",
	}

	def _strip_postgres_nuls(value):
		if isinstance(value, str):
			return _pg_nul_escape_re.sub("", value.replace("\x00", ""))
		if isinstance(value, list):
			return [_strip_postgres_nuls(item) for item in value]
		if isinstance(value, tuple):
			return tuple(_strip_postgres_nuls(item) for item in value)
		if isinstance(value, dict):
			return {
				_strip_postgres_nuls(key) if isinstance(key, str) else key:
				_strip_postgres_nuls(item)
				for key, item in value.items()
			}
		return value

	def _coerce_spend_log_json_field(value):
		value = _strip_postgres_nuls(value)
		if value is None:
			return {}
		if isinstance(value, str):
			cleaned = value.strip()
			if not cleaned:
				return {}
			try:
				parsed = json.loads(cleaned)
			except (TypeError, ValueError):
				return {"_litellm_recovered_text": value}
			if isinstance(parsed, dict):
				return parsed
			if isinstance(parsed, list):
				return json.dumps(parsed)
			return {"_litellm_recovered_value": parsed}
		return value

	def _prepare_spend_log_json_fields(data):
		data = _strip_postgres_nuls(data)
		if not isinstance(data, dict):
			return data
		for key in _spend_log_json_fields:
			if key in data:
				data[key] = _coerce_spend_log_json_field(data[key])
		return data

	def _patched_jsonify_object(self, data):
		return _original_jsonify_object(self, _prepare_spend_log_json_fields(data))

	_proxy_utils.PrismaClient.jsonify_object = _patched_jsonify_object
	logger.info("Patched LiteLLM spend-log serialization for PostgreSQL-safe JSON fields.")
except Exception as e:
	logger.error(f"Failed to patch LiteLLM spend-log serialization: {e}", exc_info=True)

# 13. Ensure the local ccproxy callback remains registered. LiteLLM may rebuild
#     `litellm.callbacks` while installing built-in proxy hooks; when that
#     happens, the configured `ccproxy_callback.ccproxy_handler` can disappear
#     even though `litellm_settings.callbacks` contains it.
try:
	def _ensure_ccproxy_callback_registered():
		import litellm as _litellm
		from ccproxy_callback import ccproxy_handler

		callbacks = _litellm.callbacks if isinstance(_litellm.callbacks, list) else []
		if any(callback is ccproxy_handler for callback in callbacks):
			return

		callbacks.append(ccproxy_handler)
		_litellm.callbacks = callbacks
		if not getattr(_ensure_ccproxy_callback_registered, "_logged", False):
			logger.info("Registered local ccproxy callback on LiteLLM callbacks.")
			setattr(_ensure_ccproxy_callback_registered, "_logged", True,)

	def _ensure_claude_aware_compression_callback_registered():
		import litellm as _litellm
		from claude_aware_compression import claude_aware_compression as _compression_callback

		callbacks = _litellm.callbacks if isinstance(_litellm.callbacks, list) else []
		if any(callback is _compression_callback for callback in callbacks):
			return

		callbacks.append(_compression_callback)
		_litellm.callbacks = callbacks
		if not getattr(_ensure_claude_aware_compression_callback_registered, "_logged", False):
			logger.info("Registered local claude_aware_compression callback on LiteLLM callbacks.")
			setattr(_ensure_claude_aware_compression_callback_registered, "_logged", True)

	_ensure_ccproxy_callback_registered()
	_ensure_claude_aware_compression_callback_registered()

	from litellm.proxy.proxy_server import app

	class _CCProxyCallbackRegistrationMiddleware:
		"""Keep local callbacks present and ensure correct HTTPS scheme for proxy redirects."""
		def __init__(self, asgi_app):
			self.asgi_app = asgi_app

		async def __call__(self, scope, receive, send):
			if scope.get("type") == "http":
				_ensure_ccproxy_callback_registered()
				_ensure_claude_aware_compression_callback_registered()
				headers = dict(scope.get("headers", []))
				proto = headers.get(b"x-forwarded-proto", b"").lower()
				host = headers.get(b"host", b"").lower()
				if proto == b"https" or b"tailscale" in host or b"ts.net" in host:
					scope["scheme"] = "https"

				async def _send_wrapper(message):
					if message.get("type") == "http.response.start":
						msg_headers = list(message.get("headers", []))
						new_headers = []
						for name, value in msg_headers:
							if name.lower() == b"location":
								loc_str = value.decode("utf-8", errors="replace")
								if loc_str.startswith("http://") and (b"ts.net" in host or b"tailscale" in host or proto == b"https"):
									loc_str = "https://" + loc_str[7:]
									value = loc_str.encode("utf-8")
							new_headers.append((name, value))
						message["headers"] = new_headers
					await send(message)

				await self.asgi_app(scope, receive, _send_wrapper)
				return
			await self.asgi_app(scope, receive, send)

	app.add_middleware(_CCProxyCallbackRegistrationMiddleware)
	logger.info("Installed ccproxy callback registration guard with HTTPS scheme correction.")
except Exception as e:
	logger.error(f"Failed to install ccproxy callback registration guard: {e}", exc_info=True)

# 14. Final route guard for Claude primary aliases. ccproxy metadata can be
#     correct while LiteLLM's later route handoff still carries another model
#     group. Patch the shared `route_request` reference so the last handoff
#     honors `ccproxy_litellm_model` for the primary Claude routes.
try:
	_CLAUDE_PRIMARY_MODEL_GROUPS = {
		"claude-chat",
		"claude-haiku-4-5-20251001",
		"claude-opus-4-8",
		"claude-opus-4-8[1m]",
		"opus[1m]",
		"claude-sonnet-4-6",
		"claude-opus-5",
		"claude-opus-5[1m]",
		"claude-sonnet-5",
		"claude-sonnet-5[1m]",
		"claude-fable-5",
	}
	_ORIGINAL_ROUTE_REQUEST = None

	def _trust_request_model_group_for_route_guard():
		return os.getenv("CCPROXY_TRUST_REQUEST_MODEL_GROUP", "").lower() in {
			"1",
			"true",
			"yes",
			"on",
		}

	def _restore_claude_primary_route_model(data):
		if _trust_request_model_group_for_route_guard() or not isinstance(data, dict):
			return None
		metadata = data.get("metadata")
		if not isinstance(metadata, dict):
			metadata = {}
		litellm_metadata = data.get("litellm_metadata")
		if not isinstance(litellm_metadata, dict):
			litellm_metadata = {}
		target = (
			metadata.get("ccproxy_litellm_model")
			or litellm_metadata.get("ccproxy_litellm_model")
		)
		if target not in _CLAUDE_PRIMARY_MODEL_GROUPS:
			proxy_request = data.get("proxy_server_request")
			if isinstance(proxy_request, dict):
				body = proxy_request.get("body")
				body_metadata = (
					body.get("metadata")
					if isinstance(body, dict) and isinstance(body.get("metadata"), dict)
					else {}
				)
				raw_model = (
					proxy_request.get("model")
					or (body.get("model") if isinstance(body, dict) else None)
					or body_metadata.get("ccproxy_litellm_model")
				)
				if raw_model in _CLAUDE_PRIMARY_MODEL_GROUPS:
					target = raw_model
		if target not in _CLAUDE_PRIMARY_MODEL_GROUPS:
			return None
		if data.get("model") != target:
			logger.info(
				"Restoring Claude primary route before LiteLLM routing: %s -> %s",
				data.get("model"),
				target,
			)
		data["model"] = target
		metadata["model_group"] = target
		metadata["ccproxy_litellm_model"] = target
		data["metadata"] = metadata
		if litellm_metadata:
			litellm_metadata["model_group"] = target
			litellm_metadata["ccproxy_litellm_model"] = target
			data["litellm_metadata"] = litellm_metadata
		return target

	async def _patched_route_request(data, llm_router, user_model, route_type, **kwargs):
		target = _restore_claude_primary_route_model(data)
		if target is not None:
			user_model = None
		return await _ORIGINAL_ROUTE_REQUEST(
			data=data,
			llm_router=llm_router,
			user_model=user_model,
			route_type=route_type,
			**kwargs,
		)

	def _ensure_route_request_patched():
		global _ORIGINAL_ROUTE_REQUEST
		from litellm.proxy import route_llm_request as _route_llm_request
		from litellm.proxy import common_request_processing as _common_request_processing

		current = _route_llm_request.route_request
		if current is not _patched_route_request:
			_ORIGINAL_ROUTE_REQUEST = current
			_route_llm_request.route_request = _patched_route_request
		if _common_request_processing.route_request is not _patched_route_request:
			if _ORIGINAL_ROUTE_REQUEST is None:
				_ORIGINAL_ROUTE_REQUEST = _common_request_processing.route_request
			_common_request_processing.route_request = _patched_route_request
		if not getattr(_ensure_route_request_patched, "_logged", False):
			logger.info("Patched LiteLLM route_request for Claude primary route restoration.")
			setattr(_ensure_route_request_patched, "_logged", True)

	_ensure_route_request_patched()
	from litellm.proxy.proxy_server import app

	class _ClaudePrimaryRouteGuardMiddleware:
		"""Reapply the route_request patch if LiteLLM refreshes module globals."""
		def __init__(self, asgi_app):
			self.asgi_app = asgi_app

		async def __call__(self, scope, receive, send):
			if scope.get("type") == "http":
				_ensure_route_request_patched()
			await self.asgi_app(scope, receive, send)

	app.add_middleware(_ClaudePrimaryRouteGuardMiddleware)
except Exception as e:
	logger.error(f"Failed to patch Claude primary route guard: {e}", exc_info=True)

# 12. Patch exception_type to translate Cato VPN blocks and dynamic context window errors
try:
	import litellm
	import litellm.exceptions
	import litellm.litellm_core_utils.exception_mapping_utils as _mapping_mod

	_original_exception_type = _mapping_mod.exception_type

	def _patched_exception_type(*args, **kwargs):
		original_exception = kwargs.get("original_exception")
		if not original_exception and len(args) > 2:
			original_exception = args[2]
			
		model = kwargs.get("model")
		if not model and len(args) > 0:
			model = args[0]
			
		custom_llm_provider = kwargs.get("custom_llm_provider")
		if not custom_llm_provider and len(args) > 1:
			custom_llm_provider = args[1]

		if original_exception is not None:
			err_msg = str(original_exception).lower()
			
			# Check for Cato Networks VPN blocks
			if (
				"powered by cato networks" in err_msg
				or "blocked access to website" in err_msg
				or "access notification" in err_msg
				or "<!doctype html>" in err_msg
			):
				logger.warning(f"[CATO_BLOCK_GUARD] Cato VPN block detected for model={model}. Mapping to APIConnectionError.")
				raise litellm.exceptions.APIConnectionError(
					message=f"Request blocked by Cato Networks VPN: {original_exception}",
					model=model or "",
					llm_provider=custom_llm_provider or "",
				)
				
			# Check for context window exceeded errors
			if (
				"exceeds the context window" in err_msg
				or "max_tokens must be at least 1, got" in err_msg
				or "context length" in err_msg
				or "context window" in err_msg
				or "too many tokens" in err_msg
			):
				logger.warning(f"[CONTEXT_LIMIT_GUARD] Context limit exceeded detected for model={model}. Mapping to ContextWindowExceededError.")
				raise litellm.exceptions.ContextWindowExceededError(
					message=f"Context window exceeded: {original_exception}",
					model=model or "",
					llm_provider=custom_llm_provider or "",
				)

		return _original_exception_type(*args, **kwargs)

	_mapping_mod.exception_type = _patched_exception_type
	litellm.exception_type = _patched_exception_type
	logger.info("Successfully patched LiteLLM exception_type for Cato VPN blocks and context window errors.")
except Exception as e:
	logger.error(f"Failed to patch exception_type: {e}", exc_info=True)

# 13. Patch ChatGPTToolCallNormalizer to prevent dropping tool call arguments
try:
	from litellm.llms.chatgpt.chat.streaming_utils import ChatGPTToolCallNormalizer

	_original_normalize = ChatGPTToolCallNormalizer._normalize

	def _patched_normalize(self, chunk):
		if not chunk.choices:
			return chunk

		delta = chunk.choices[0].delta
		if delta is None or not delta.tool_calls:
			return chunk

		normalized = []
		for tc in delta.tool_calls:
			# Get id safely
			if isinstance(tc, dict):
				tc_id = tc.get("id")
			else:
				tc_id = getattr(tc, "id", None)

			has_function_delta = False
			if isinstance(tc, dict):
				tc_fun = tc.get("function")
				if isinstance(tc_fun, dict) and (tc_fun.get("arguments") is not None or tc_fun.get("name") is not None):
					has_function_delta = True
			else:
				tc_fun = getattr(tc, "function", None)
				if tc_fun is not None and (getattr(tc_fun, "arguments", None) is not None or getattr(tc_fun, "name", None) is not None):
					has_function_delta = True

			if tc_id and tc_id not in self._seen_ids:
				# New tool call — assign correct index
				self._seen_ids[tc_id] = self._next_index
				if isinstance(tc, dict):
					tc["index"] = self._next_index
				else:
					tc.index = self._next_index
				self._last_id = tc_id
				self._next_index += 1
				normalized.append(tc)
			elif tc_id and tc_id in self._seen_ids:
				if has_function_delta:
					# Keep continuation delta even if it has an id
					if isinstance(tc, dict):
						tc["index"] = self._seen_ids[tc_id]
					else:
						tc.index = self._seen_ids[tc_id]
					self._last_id = tc_id
					normalized.append(tc)
				else:
					# Duplicate "closing" chunk — skip it
					continue
			else:
				# Continuation delta (id=None) — fix index
				if self._last_id:
					if isinstance(tc, dict):
						tc["index"] = self._seen_ids[self._last_id]
					else:
						tc.index = self._seen_ids[self._last_id]
				normalized.append(tc)

		if not normalized:
			return None  # all tool_calls were duplicates, skip chunk

		delta.tool_calls = normalized
		return chunk

	ChatGPTToolCallNormalizer._normalize = _patched_normalize
	logger.info("Successfully patched ChatGPTToolCallNormalizer._normalize to preserve stream arguments.")
except Exception as e:
	logger.error(f"Failed to patch ChatGPTToolCallNormalizer: {e}", exc_info=True)

