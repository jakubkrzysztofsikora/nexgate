"""Local ccproxy shims used by LiteLLM's file-based loader."""

import asyncio
import json
import logging
import math
import os
import re
import subprocess
import time
import html
import hashlib
import uuid
import queue
import threading
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

try:
	from ccproxy.config import CCProxyConfig, get_config
	_original_load_credentials = getattr(CCProxyConfig, "_original_load_credentials", getattr(CCProxyConfig, "_load_credentials", None))
	if _original_load_credentials is not None:
		CCProxyConfig._original_load_credentials = _original_load_credentials

		def _patched_load_credentials(self):
			try:
				_original_load_credentials(self)
			except Exception as exc:
				if not hasattr(self, "_oat_values") or self._oat_values is None:
					self._oat_values = {}
				if not hasattr(self, "_oat_user_agents") or self._oat_user_agents is None:
					self._oat_user_agents = {}
				for provider in getattr(self, "oat_sources", {}):
					if provider not in self._oat_values:
						self._oat_values[provider] = ""

		CCProxyConfig._load_credentials = _patched_load_credentials
except Exception:
	from ccproxy.config import get_config

from ccproxy.handler import CCProxyHandler
from litellm.litellm_core_utils.get_llm_provider_logic import get_llm_provider

_LITELLM_CONFIG_PATH = os.getenv("LITELLM_PROXY_CONFIG_PATH", "/app/config.yaml")
_MAX_COMPLETION_TOKENS_LIMIT = int(os.environ.get("MAX_COMPLETION_TOKENS_LIMIT", "16384"))
_MODEL_NAME_TO_PROVIDER: dict[str, str] = {}
_MODEL_NAME_MAP_MTIME: float | None = None

# Lifecycle telemetry is intentionally low-cardinality. Request/session IDs and
# tool names are hashed in the JSONL audit log and never become Prometheus labels.
_SAVINGS_EVENT_SCHEMA = 1
_SAVINGS_EVENT_PATH = os.getenv(
	"CCPROXY_SAVINGS_EVENT_LOG", "/host/logs/savings_events.jsonl"
)
_SAVINGS_EVENT_SALT = os.getenv("CCPROXY_SAVINGS_EVENT_SALT", "")
_SAVINGS_EVENTS_ENABLED = bool(_SAVINGS_EVENT_SALT)
_SAVINGS_EVENT_MAX_BYTES = int(os.getenv("CCPROXY_SAVINGS_EVENT_MAX_BYTES", str(50 * 1024 * 1024)))
_SAVINGS_EVENT_QUEUE: queue.Queue[str] = queue.Queue(maxsize=int(os.getenv("CCPROXY_SAVINGS_EVENT_QUEUE_SIZE", "1000")))
_SAVINGS_EVENT_DROPPED = 0
_SAVINGS_EVENT_WRITER_STARTED = False
_HARNESS_HEADER = os.getenv("CCPROXY_HARNESS_HEADER", "x-sovereign-harness").lower()
_HARNESS_TOKEN = os.getenv("CCPROXY_HARNESS_TOKEN", "")
_KNOWN_HARNESSES = frozenset({"claude_code", "codex_native", "codex_bridged"})


def _hash_telemetry_identifier(value: Any) -> str | None:
	"""Return a stable, non-reversible identifier for correlation only."""
	if not isinstance(value, str) or not value:
		return None
	if not _SAVINGS_EVENTS_ENABLED:
		return None
	key = hashlib.sha256(_SAVINGS_EVENT_SALT.encode("utf-8")).digest()
	return hashlib.blake2s(
		value.encode("utf-8"), key=key, digest_size=12
	).hexdigest()


def _trusted_harness(data: dict[str, Any]) -> str:
	"""Accept harness attribution only from the configured authenticated header."""
	request = data.get("proxy_server_request")
	headers = request.get("headers") if isinstance(request, dict) else None
	if not isinstance(headers, dict) or not _HARNESS_TOKEN:
		return "unknown"
	value = next((v for k, v in headers.items() if str(k).lower() == _HARNESS_HEADER), None)
	if not isinstance(value, str):
		return "unknown"
	# Header format: "harness:shared-token". Reject shape-only attribution.
	harness, sep, token = value.partition(":")
	return harness if sep and token == _HARNESS_TOKEN and harness in _KNOWN_HARNESSES else "unknown"


def _emit_savings_event(data: dict[str, Any], outcome: str, **extra: Any) -> None:
	"""Persist a bounded lifecycle record; telemetry failure never affects a call."""
	global _SAVINGS_EVENT_DROPPED
	try:
		if not _SAVINGS_EVENTS_ENABLED:
			return
		metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
		tools = data.get("tools")
		event = {
			"schema": _SAVINGS_EVENT_SCHEMA,
			"timestamp": datetime.now(timezone.utc).isoformat(),
			"outcome": outcome,
			"harness": _trusted_harness(data),
			"request_id": _hash_telemetry_identifier(metadata.get("request_id")),
			"session_id": _hash_telemetry_identifier(metadata.get("session_id")),
			"root_request_id": _hash_telemetry_identifier(metadata.get("ccproxy_root_request_id")),
			"requested_model": str(metadata.get("ccproxy_requested_model", data.get("model", "unknown")))[:160],
			"resolved_model": str(data.get("model", "unknown"))[:160],
			"responses_shape": bool(metadata.get("ccproxy_responses_shape")),
			"tool_count": len(tools) if isinstance(tools, list) else 0,
			"techniques": {
				"tool_search": bool(metadata.get("ccproxy_tool_search_applied")),
				"utility_route": bool(metadata.get("ccproxy_utility_route")),
				"terse": bool(metadata.get("ccproxy_terse_applied")),
			},
		}
		event.update({k: v for k, v in extra.items() if v is not None and isinstance(v, (int, float, bool)) or isinstance(v, str) and len(v) <= 160})
		_SAVINGS_EVENT_QUEUE.put_nowait(json.dumps(event, separators=(",", ":")) + "\n")
	except queue.Full:
		_SAVINGS_EVENT_DROPPED += 1
		if _SAVINGS_EVENT_DROPPED == 1 or _SAVINGS_EVENT_DROPPED % 100 == 0:
			_TOOL_VALIDATION_LOGGER.warning("savings telemetry queue full; dropped=%d", _SAVINGS_EVENT_DROPPED)
	except Exception:
		_TOOL_VALIDATION_LOGGER.debug("savings event emission failed", exc_info=True)


def _savings_event_writer() -> None:
	"""Single daemon writer keeps filesystem latency off LiteLLM async hooks."""
	while True:
		line = _SAVINGS_EVENT_QUEUE.get()
		try:
			os.makedirs(os.path.dirname(_SAVINGS_EVENT_PATH) or ".", exist_ok=True)
			if os.path.exists(_SAVINGS_EVENT_PATH) and os.path.getsize(_SAVINGS_EVENT_PATH) >= _SAVINGS_EVENT_MAX_BYTES:
				rotated = f"{_SAVINGS_EVENT_PATH}.{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
				os.replace(_SAVINGS_EVENT_PATH, rotated)
				_TOOL_VALIDATION_LOGGER.warning("rotated savings telemetry log: %s", rotated)
			with open(_SAVINGS_EVENT_PATH, "a", encoding="utf-8") as fh:
				fh.write(line)
		except Exception:
			_TOOL_VALIDATION_LOGGER.warning("savings telemetry writer failed", exc_info=True)
		finally:
			_SAVINGS_EVENT_QUEUE.task_done()


def _start_savings_event_writer() -> None:
	global _SAVINGS_EVENT_WRITER_STARTED
	if _SAVINGS_EVENTS_ENABLED and not _SAVINGS_EVENT_WRITER_STARTED:
		threading.Thread(target=_savings_event_writer, name="savings-event-writer", daemon=True).start()
		_SAVINGS_EVENT_WRITER_STARTED = True


_start_savings_event_writer()


def _load_model_name_to_provider_map() -> dict[str, str]:
	"""Build a `model_name -> provider` map from the proxy yaml.

	LiteLLM callbacks receive `data["model"]` as the proxy-level alias
	(e.g. "mistral", "kimi", "glm-5.1", "scaleway-devstral"). `get_llm_provider`
	cannot resolve those aliases because they have no provider prefix.
	Without a provider name we cannot run `_sanitize_messages`, so Anthropic
	`thinking_blocks` leak into OpenAI/Mistral-compatible endpoints and crash
	the request. Reading the live proxy config gives us a deterministic alias
	lookup that stays in sync with whatever `make start-local` rendered into
	config/litellm.yaml from the template.

	Cached per config-file mtime so edits + container restarts pick up
	automatically.
	"""
	global _MODEL_NAME_TO_PROVIDER, _MODEL_NAME_MAP_MTIME

	try:
		mtime = os.path.getmtime(_LITELLM_CONFIG_PATH)
	except OSError:
		return _MODEL_NAME_TO_PROVIDER

	if _MODEL_NAME_MAP_MTIME == mtime and _MODEL_NAME_TO_PROVIDER:
		return _MODEL_NAME_TO_PROVIDER

	try:
		import yaml

		with open(_LITELLM_CONFIG_PATH, "r", encoding="utf-8") as handle:
			config = yaml.safe_load(handle) or {}
	except Exception:
		return _MODEL_NAME_TO_PROVIDER

	new_map: dict[str, str] = {}
	for entry in config.get("model_list", []) or []:
		if not isinstance(entry, dict):
			continue
		model_name = entry.get("model_name")
		litellm_params = entry.get("litellm_params") or {}
		underlying = (
			litellm_params.get("model") if isinstance(litellm_params, dict) else None
		)
		if not isinstance(model_name, str) or not isinstance(underlying, str):
			continue
		try:
			_, provider, _, _ = get_llm_provider(
				model=underlying,
				custom_llm_provider=(
					litellm_params.get("custom_llm_provider")
					if isinstance(litellm_params, dict)
					else None
				),
				api_base=(
					litellm_params.get("api_base")
					if isinstance(litellm_params, dict)
					else None
				),
			)
		except Exception:
			provider = None
		if provider:
			new_map[model_name] = provider

	_MODEL_NAME_TO_PROVIDER = new_map
	_MODEL_NAME_MAP_MTIME = mtime
	return _MODEL_NAME_TO_PROVIDER


# Populate at import so first request doesn't pay the parse cost.
_load_model_name_to_provider_map()


class _SuppressImageTokenCountWarning(logging.Filter):
	"""Drops the noisy `Invalid content item type: image` warning emitted by
	LiteLLM's router pre-call token counter on Anthropic-native image blocks.

	Root cause: router uses OpenAI-format token counter (expects `image_url`)
	but `/v1/messages` passthrough carries Anthropic-format `image` blocks.
	Router falls back to default deployment list either way, so the warning
	is informational, not actionable.
	"""

	def filter(self, record: logging.LogRecord) -> bool:
		message = record.getMessage()
		if "Invalid content item type: image" in message:
			return False
		if "failed to count tokens" in message and "image" in message:
			return False
		return True


logging.getLogger("LiteLLM Router").addFilter(_SuppressImageTokenCountWarning())


def _strip_null_bytes(value: Any) -> Any:
	"""Recursively strip `\\u0000` from strings nested in dict/list/scalar.

	Postgres rejects `\\u0000` in `text`/`jsonb` (SQLSTATE 22P05). LiteLLM
	spend-log persistence runs the same `data["messages"]` through Prisma,
	so any client payload carrying stray null bytes (binary leak, encoded
	image fragment, malformed tool result) crashes the spend job.
	"""
	if isinstance(value, str):
		if "\x00" in value:
			return value.replace("\x00", "")
		return value
	if isinstance(value, dict):
		return {key: _strip_null_bytes(item) for key, item in value.items()}
	if isinstance(value, list):
		return [_strip_null_bytes(item) for item in value]
	return value


def _get_oauth_refresh_ttl_seconds() -> float:
	raw_ttl = os.getenv("CCPROXY_OAUTH_REFRESH_SECONDS", "60")
	try:
		return max(0.0, float(raw_ttl))
	except ValueError:
		return 60.0


_OAUTH_REFRESH_TTL_SECONDS = _get_oauth_refresh_ttl_seconds()
_OAUTH_TOKEN_CACHE: dict[str, tuple[str, float]] = {}
_ANTHROPIC_EMPTY_TEXT_PLACEHOLDER = "[empty]"


def _stream_buffer_timeout_seconds() -> float:
	raw_timeout = os.getenv(
		"CCPROXY_STREAM_BUFFER_TIMEOUT_SECONDS",
		os.getenv("ANTHROPIC_MESSAGES_FIRST_CHUNK_TIMEOUT", "30"),
	)
	try:
		return max(0.0, float(raw_timeout))
	except (TypeError, ValueError):
		return 30.0


def _anthropic_ping_event() -> str:
	return 'event: ping\ndata: {"type":"ping"}\n\n'

_CLAUDE_PRIMARY_PROVIDER_MODELS = {
	"claude-chat": "anthropic/claude-haiku-4-5-20251001",
	"claude-haiku-4-5-20251001": "anthropic/claude-haiku-4-5-20251001",
	"claude-opus-4-8": "anthropic/claude-opus-4-8",
	"claude-opus-4-8[1m]": "anthropic/claude-opus-4-8",
	"opus[1m]": "anthropic/claude-opus-4-8",
	"claude-opus-5": "anthropic/claude-opus-5",
	"claude-opus-5[1m]": "anthropic/claude-opus-5",
	"claude-sonnet-4-6": "anthropic/claude-sonnet-4-6",
	"claude-sonnet-5": "anthropic/claude-sonnet-5",
	"claude-sonnet-5[1m]": "anthropic/claude-sonnet-5",
	"claude-fable-5": "anthropic/claude-fable-5",
}
_CLAUDE_PRIMARY_MODEL_GROUPS = set(_CLAUDE_PRIMARY_PROVIDER_MODELS)
_CLAUDE_PRIMARY_PROVIDER_MODEL_VALUES = set(_CLAUDE_PRIMARY_PROVIDER_MODELS.values())
_CLAUDE_ONE_MILLION_CONTEXT_MODELS = {
	"claude-opus-4-8[1m]",
	"claude-opus-5[1m]",
	"opus[1m]",
	"claude-sonnet-5[1m]",
}
_CLAUDE_ONE_MILLION_CONTEXT_BETA = "context-1m-2025-08-07"
_ANTHROPIC_API_BASE = "https://api.anthropic.com"

# Tool-search (deferred loading) feature flag. Default off; the injector is a
# no-op until CCPROXY_TOOL_SEARCH matches. Every flip busts the tool prefix
# cache once per open session, so flips happen in low-volume windows.
_TOOL_SEARCH_FLAG = os.getenv("CCPROXY_TOOL_SEARCH", "").strip().lower()
_TOOL_SEARCH_ENABLED = _TOOL_SEARCH_FLAG in {"1", "true", "yes", "on"}
_TOOL_SEARCH_MIN = int(os.getenv("CCPROXY_TOOL_SEARCH_MIN", "30"))
# Anthropic-only; the beta token is the version the spike confirmed the API
# accepts (429 = valid, not a reject). Merged alongside context-1m, never over.
_TOOL_SEARCH_BETA = "advanced-tool-use-2025-11-20"
# Model gate: opus-4-8 is the safe passthrough rollout target. Other models stay
_TOOL_SEARCH_MODELS = frozenset({
	"claude-opus-4-8", "claude-opus-4-8[1m]",
	"claude-opus-5", "claude-opus-5[1m]",
	"claude-sonnet-5", "claude-sonnet-5[1m]",
	"claude-fable-5",
})
# Always-loaded core tools. Discovery is a search hop, so tools called every
# turn stay non-deferred. Kept well under the ~30-50 tool count where selection
# degrades. Promotion is a deploy-time code change, never a live mutation.
_TOOL_SEARCH_HOT_TOOLS = frozenset({
    "Read", "Edit", "Write", "Bash", "Grep", "Glob",
    "Task", "Agent", "TaskUpdate", "TodoWrite",
    "Search", "WebSearch", "Web Search", "ReadURL", "Fetch",
})
_TOOL_SEARCH_TOOL = {"type": "tool_search_tool_bm25_20251119", "name": "tool_search_tool_bm25"}


def _sticky_experiment_cohort(data: dict[str, Any], experiment: str) -> str:
	"""Return a stable control/treatment assignment for a known session only."""
	percentage = int(os.getenv(f"CCPROXY_{experiment}_TREATMENT_PERCENT", "0"))
	if percentage <= 0:
		return "control"
	metadata = data.get("metadata")
	session_id = metadata.get("session_id") if isinstance(metadata, dict) else None
	if not isinstance(session_id, str) or not session_id:
		return "unknown"
	bucket = int(hashlib.sha256(f"{experiment}:{session_id}".encode()).hexdigest()[:8], 16) % 100
	return "treatment" if bucket < min(percentage, 100) else "control"


def _is_non_empty_string(value: Any) -> bool:
	return isinstance(value, str) and bool(value.strip())


def _non_whitespace_text(value: Any) -> str:
	if isinstance(value, str) and value.strip():
		return value
	return _ANTHROPIC_EMPTY_TEXT_PLACEHOLDER


def _trust_request_model_group() -> bool:
	return os.getenv("CCPROXY_TRUST_REQUEST_MODEL_GROUP", "").lower() in {
		"1",
		"true",
		"yes",
		"on",
	}


def _normalize_claude_metadata_model_group(data: dict[str, Any]) -> bool:
	"""Keep Claude primary aliases from being silently re-routed by stale metadata."""
	if _trust_request_model_group():
		return False

	model = data.get("model")
	if model not in _CLAUDE_PRIMARY_MODEL_GROUPS:
		return False

	metadata = data.get("metadata")
	if not isinstance(metadata, dict):
		return False

	requested_group = metadata.get("model_group")
	if not isinstance(requested_group, str) or requested_group == model:
		return False

	metadata["model_group"] = model

	proxy_request = data.get("proxy_server_request")
	if isinstance(proxy_request, dict):
		body = proxy_request.get("body")
		if isinstance(body, dict):
			body_metadata = body.get("metadata")
			if isinstance(body_metadata, dict):
				body_metadata["model_group"] = model

	_TOOL_VALIDATION_LOGGER.info(
		"claude metadata model_group normalization: requested=%s model=%s",
		requested_group,
		model,
	)
	return True


def _force_claude_primary_passthrough(
	data: dict[str, Any],
	requested_model: Any | None = None,
) -> bool:
	"""Keep Claude primary aliases on the requested LiteLLM model group."""
	if _trust_request_model_group():
		return False

	model = requested_model if requested_model is not None else data.get("model")
	if model not in _CLAUDE_PRIMARY_MODEL_GROUPS:
		return False
	provider_model = _CLAUDE_PRIMARY_PROVIDER_MODELS[model]

	metadata = data.get("metadata")
	if not isinstance(metadata, dict):
		metadata = {}
		data["metadata"] = metadata

	changed = data.get("model") != model
	data["model"] = model

	for key, value in {
		"ccproxy_alias_model": model,
		"ccproxy_model_name": "default",
		"ccproxy_litellm_model": model,
		"ccproxy_model_config": None,
		"ccproxy_is_passthrough": True,
	}.items():
		if metadata.get(key) != value:
			changed = True
			metadata[key] = value

	return changed


def _force_claude_primary_direct_provider(
	data: dict[str, Any],
	requested_model: Any | None = None,
) -> bool:
	if _trust_request_model_group():
		return False

	model = requested_model if requested_model is not None else data.get("model")
	if model not in _CLAUDE_PRIMARY_MODEL_GROUPS:
		return False
	provider_model = _CLAUDE_PRIMARY_PROVIDER_MODELS[model]

	metadata = data.get("metadata")
	if not isinstance(metadata, dict):
		metadata = {}
		data["metadata"] = metadata

	data["model"] = provider_model
	data["api_base"] = _ANTHROPIC_API_BASE
	if model in _CLAUDE_ONE_MILLION_CONTEXT_MODELS:
		extra_headers = data.get("extra_headers")
		if not isinstance(extra_headers, dict):
			extra_headers = {}
			data["extra_headers"] = extra_headers
		existing_beta = extra_headers.get("anthropic-beta")
		if isinstance(existing_beta, str) and existing_beta.strip():
			if _CLAUDE_ONE_MILLION_CONTEXT_BETA not in existing_beta:
				extra_headers["anthropic-beta"] = (
					f"{existing_beta},{_CLAUDE_ONE_MILLION_CONTEXT_BETA}"
				)
		else:
			extra_headers["anthropic-beta"] = _CLAUDE_ONE_MILLION_CONTEXT_BETA
	token = _get_live_oauth_token("anthropic")
	if token:
		data["api_key"] = token

	metadata["model_group"] = model
	metadata["ccproxy_alias_model"] = model
	metadata["ccproxy_model_name"] = "default"
	metadata["ccproxy_litellm_model"] = model
	metadata["ccproxy_provider_model"] = provider_model
	metadata["ccproxy_model_config"] = None
	metadata["ccproxy_is_passthrough"] = True
	return True


def _get_source_command(source: Any) -> str:
	if isinstance(source, str):
		return source
	if isinstance(source, dict):
		return str(source.get("command", ""))
	return str(getattr(source, "command", ""))


def _get_live_oauth_token(provider_name: str) -> str | None:
	config = get_config()
	cached_entry = _OAUTH_TOKEN_CACHE.get(provider_name)
	now = time.time()

	if provider_name == "anthropic":
		env_token = os.getenv("CLAUDE_CODE_OAUTH_TOKEN")
		if env_token:
			_OAUTH_TOKEN_CACHE[provider_name] = (env_token, now)
			return env_token

	if cached_entry and now - cached_entry[1] < _OAUTH_REFRESH_TTL_SECONDS:
		return cached_entry[0]

	source = config.oat_sources.get(provider_name)
	command = _get_source_command(source)
	if command:
		try:
			result = subprocess.run(
				command,
				shell=True,
				capture_output=True,
				text=True,
				timeout=5,
			)
			if result.returncode == 0:
				token = result.stdout.strip()
				if token:
					_OAUTH_TOKEN_CACHE[provider_name] = (token, now)
					return token
		except Exception:
			pass

	if cached_entry:
		return cached_entry[0]

	token = config.get_oauth_token(provider_name)
	if token:
		_OAUTH_TOKEN_CACHE[provider_name] = (token, now)
	return token


def _tool_search_discovered_names(block: dict[str, Any]) -> list[str]:
	"""Extract discovered-tool names from a tool-search block.

	`server_tool_use` and `tool_reference` carry a top-level `name`;
	`tool_search_tool_result` nests schemas — including `name` — under
	`content`. Collect from every shape so the fallback text note names the
	actual tools instead of "unknown".
	"""
	names: list[str] = []
	name = block.get("name") or block.get("tool_name") or block.get("id")
	if isinstance(name, str) and name:
		names.append(name)
	content = block.get("content")
	if isinstance(content, list):
		for item in content:
			if not isinstance(item, dict):
				continue
			item_name = item.get("name") or item.get("tool_name") or item.get("id")
			if isinstance(item_name, str) and item_name and item_name not in names:
				names.append(item_name)
	return names


def _sanitize_content_blocks(content: Any, provider_name: str) -> Any:
	if not isinstance(content, list):
		if provider_name == "anthropic":
			return _non_whitespace_text(content)
		if content == "":
			return _ANTHROPIC_EMPTY_TEXT_PLACEHOLDER
		return content

	sanitized_blocks: list[Any] = []
	for block in content:
		if not isinstance(block, dict):
			sanitized_blocks.append(block)
			continue

		block_type = block.get("type")
		is_non_anthropic_image = block_type in {"image", "image_url"} and provider_name == "deepseek"
		if block_type == "text":
			if provider_name == "anthropic" and not _is_non_empty_string(block.get("text")):
				updated_block = dict(block)
				updated_block["text"] = _ANTHROPIC_EMPTY_TEXT_PLACEHOLDER
				sanitized_blocks.append(updated_block)
			elif block.get("text") == "":
				updated_block = dict(block)
				updated_block["text"] = _ANTHROPIC_EMPTY_TEXT_PLACEHOLDER
				sanitized_blocks.append(updated_block)
			else:
				sanitized_blocks.append(block)
			continue

		if block_type == "thinking":
			if provider_name != "anthropic":
				# Preserve reasoning trace for fallback models by converting
				# thinking blocks to plain text. Without this the model loses
				# its own reasoning from previous turns and forgets what it
				# was doing.
				thinking_text = block.get("thinking")
				if _is_non_empty_string(thinking_text):
					sanitized_blocks.append(
						{"type": "text", "text": thinking_text}
					)
				continue
			if not _is_non_empty_string(block.get("thinking")):
				continue
			if not _is_non_empty_string(block.get("signature")):
				continue
			sanitized_blocks.append(block)
			continue

		if block_type == "redacted_thinking" and provider_name != "anthropic":
			continue

		if is_non_anthropic_image:
			image_type = "image"
			if block_type == "image":
				source = block.get("source")
				if isinstance(source, dict):
					image_type = source.get("media_type", "") or image_type
			else:
				source = block.get("image_url")
				if isinstance(source, dict):
					url = source.get("url")
					if isinstance(url, str) and url.startswith("data:"):
						mime_part = url[5:].split(";", 1)[0]
						if mime_part:
							image_type = mime_part
			sanitized_blocks.append(
				{
					"type": "text",
					"text": f"[Image input omitted for provider {provider_name}: {image_type}]",
				}
			)
			continue

		if block_type == "image" and provider_name != "anthropic":
			source = block.get("source")
			if isinstance(source, dict):
				media_type = source.get("media_type", "")
				data_b64 = source.get("data", "")
				if media_type and data_b64:
					sanitized_blocks.append({
						"type": "image_url",
						"image_url": {
							"url": f"data:{media_type};base64,{data_b64}"
						}
					})
					continue

		if block_type == "document" and provider_name != "anthropic":
			source = block.get("source")
			if isinstance(source, dict):
				media_type = source.get("media_type", "")
				data_b64 = source.get("data", "")
				if media_type.startswith("text/") and data_b64:
					try:
						import base64
						decoded_text = base64.b64decode(data_b64).decode("utf-8", errors="replace")
						sanitized_blocks.append({"type": "text", "text": decoded_text})
						continue
					except Exception:
						pass
				sanitized_blocks.append({
					"type": "text",
					"text": f"[Document: {media_type}, size: {len(data_b64)} bytes]"
				})
				continue

		# Tool-search blocks are Anthropic-only. When a fallback routes to a
		# non-Claude provider on a later turn, `server_tool_use` /
		# `tool_search_tool_result` / `tool_reference` sitting in message history
		# reach an OpenAI-compatible endpoint that doesn't understand them ->
		# 400 or silent mangling. Flatten to a short text note, mirroring the
		# thinking->text conversion.
		if provider_name != "anthropic" and block_type in {
			"server_tool_use",
			"tool_search_tool_result",
			"tool_reference",
		}:
			names = _tool_search_discovered_names(block)
			label = ",".join(names) if names else "unknown"
			sanitized_blocks.append({"type": "text", "text": f"[tool search: discovered {label}]"})
			continue

		sanitized_blocks.append(block)

	if content and not sanitized_blocks:
		return [{"type": "text", "text": _ANTHROPIC_EMPTY_TEXT_PLACEHOLDER}]

	return sanitized_blocks


def _scan_tool_search_discovery(payload: dict[str, Any]) -> tuple[list[str], int]:
	"""Scan one stream payload for tool-search discovery blocks.

	Handles both the passthrough `content_block_start` shape and the
	non-passthrough ModelResponse shape where blocks are nested under
	`message.content` / `choices[*].message.content`. Returns (discovered
	names, estimated re-embedding tokens).
	"""
	names: list[str] = []
	tokens = 0
	blocks: list[Any] = []
	if payload.get("type") == "content_block_start":
		block = payload.get("content_block")
		if isinstance(block, dict):
			blocks.append(block)
	elif payload.get("type") == "tool_search_tool_result":
		tokens += len(json.dumps(payload)) // 4
		names.extend(_tool_search_discovered_names(payload))
	else:
		# Nested ModelResponse: message.content / choices[*].message.content.
		message = payload.get("message")
		if isinstance(message, dict) and isinstance(message.get("content"), list):
			blocks.extend(b for b in message["content"] if isinstance(b, dict))
		for choice in payload.get("choices") or []:
			if not isinstance(choice, dict):
				continue
			msg = choice.get("message")
			if isinstance(msg, dict) and isinstance(msg.get("content"), list):
				blocks.extend(b for b in msg["content"] if isinstance(b, dict))
	for block in blocks:
		if block.get("type") == "server_tool_use":
			names.extend(_tool_search_discovered_names(block))
		elif block.get("type") == "tool_search_tool_result":
			tokens += len(json.dumps(block)) // 4
			names.extend(_tool_search_discovered_names(block))
		elif block.get("type") == "tool_reference":
			names.extend(_tool_search_discovered_names(block))
	return names, tokens


def _log_tool_search_discovery(buffered: list[Any], request_data: dict[str, Any]) -> None:
	"""Log discovered-tool names + tokens so the re-embedding cost is measurable.

	Discovered tools enter the assistant turn and are re-sent in `messages[]`
	on every subsequent turn — post-breakpoint, paid at full input rate. To net
	that against the prefix-shrink saving we need names and an estimated token
	count per discovery, keyed by session_id so per-session totals can be
	summed. Best-effort: never raises, never breaks streaming.
	"""
	if not _TOOL_SEARCH_ENABLED:
		return
	if os.getenv("CCPROXY_TOOL_SEARCH_TREATMENT_PERCENT") and _sticky_experiment_cohort(request_data, "TOOL_SEARCH") != "treatment":
		return
	discovered: list[str] = []
	tokens = 0
	for item in buffered:
		if isinstance(item, dict):
			payload = item.get("data") or item
			if not isinstance(payload, dict):
				continue
			names, toks = _scan_tool_search_discovery(payload)
			for name in names:
				if name not in discovered:
					discovered.append(name)
			tokens += toks
			continue
		# Raw SSE text frame: scan for the discovery block types by name.
		text = _stream_item_text(item)
		if not text or ('"server_tool_use"' not in text and '"tool_search_tool_result"' not in text):
			continue
		try:
			for frame in text.split("\n\n"):
				for line in frame.splitlines():
					if line.startswith("data:"):
						payload = json.loads(line.split("data:", 1)[1].strip())
						if isinstance(payload, dict):
							names, toks = _scan_tool_search_discovery(payload)
							for name in names:
								if name not in discovered:
									discovered.append(name)
							tokens += toks
		except Exception:
			continue
	if not discovered:
		return
	_emit_savings_event(request_data, "tool_search_discovery", discovery_count=len(discovered), discovery_tokens=tokens)


def _strip_tool_search_artifacts(kwargs: dict[str, Any]) -> None:
	"""Remove Anthropic-only tool-search artifacts from a non-Claude fallback.

	LiteLLM can route opus-4-8 to deepseek/glm/kimi on long-context turns via
	`context_window_fallbacks`. The request tools[] must not carry
	`defer_loading` or `tool_search_tool_*` entries to a provider that doesn't
	understand them.
	"""
	tools = kwargs.get("tools")
	if isinstance(tools, list):
		kwargs["tools"] = [
			{k: v for k, v in t.items() if k != "defer_loading"}
			for t in tools
			if isinstance(t, dict)
			and not str(t.get("type", "")).startswith("tool_search_tool")
		]


def _model_supports_tool_search(model: str | None) -> bool:
	"""Gate tool-search to opus-4-8 only until write-side savings are proven."""
	if not isinstance(model, str):
		return False
	return model in _TOOL_SEARCH_MODELS


def _apply_tool_search_defer(data: dict[str, Any]) -> None:
	"""Inject Anthropic tool-search into a request that has enough tools.

	This is called from the pre-call hook as the LAST mutation of `tools[]`,
	after compression and system-message normalization. It marks every tool
	not in the hot-set with `defer_loading=True`, strips any client-set
	`cache_control` (defer+cache_control = hard 400), prepends the BM25
	search tool, and merges the beta header alongside context-1m.

	The injector is purely additive; when the flag is off it returns
	immediately and behaviour is byte-identical to today.
	"""
	if not _TOOL_SEARCH_ENABLED:
		return
	if os.getenv("CCPROXY_TOOL_SEARCH_TREATMENT_PERCENT") and _sticky_experiment_cohort(data, "TOOL_SEARCH") != "treatment":
		return
	if not _request_is_anthropic_shape(data):
		return
	model = data.get("model")
	if not _model_supports_tool_search(model):
		return
	tools = data.get("tools")
	if not isinstance(tools, list) or len(tools) < _TOOL_SEARCH_MIN:
		return
	# Client already opted in — don't double-inject.
	if any(
		isinstance(t, dict) and str(t.get("type", "")).startswith("tool_search_tool")
		for t in tools
	):
		return
	deferred_count = 0
	for t in tools:
		if isinstance(t, dict) and t.get("name") not in _TOOL_SEARCH_HOT_TOOLS:
			t["defer_loading"] = True
			# REQUIRED: defer_loading + cache_control = 400 (Claude Code #30920).
			t.pop("cache_control", None)
			deferred_count += 1
	data["tools"] = [*tools, _TOOL_SEARCH_TOOL]
	extra_headers = data.setdefault("extra_headers", {})
	if not isinstance(extra_headers, dict):
		extra_headers = {}
		data["extra_headers"] = extra_headers
	existing_beta = extra_headers.get("anthropic-beta")
	parts: list[str] = []
	if isinstance(existing_beta, str):
		parts.extend(p.strip() for p in existing_beta.split(",") if p.strip())
	if _TOOL_SEARCH_BETA not in parts:
		parts.append(_TOOL_SEARCH_BETA)
	if _CLAUDE_ONE_MILLION_CONTEXT_BETA not in parts:
		parts.append(_CLAUDE_ONE_MILLION_CONTEXT_BETA)
	extra_headers["anthropic-beta"] = ",".join(parts)
	_TOOL_VALIDATION_LOGGER.info(
		"ccproxy tool-search injected: model=%s total_tools=%d deferred=%d",
		model,
		len(tools) + 1,
		deferred_count,
	)
	metadata = data.get("metadata")
	if isinstance(metadata, dict):
		metadata["ccproxy_tool_search_applied"] = True


def _request_has_claude_reasoning_artifacts(data: dict[str, Any]) -> bool:
	if data.get("thinking") is not None:
		return True
	if data.get("reasoning") is not None or data.get("reasoning_effort") is not None:
		return True

	messages = data.get("messages")
	if not isinstance(messages, list):
		return False

	for message in messages:
		if not isinstance(message, dict):
			continue
		if message.get("thinking_blocks"):
			return True

		content = message.get("content")
		if not isinstance(content, list):
			continue

		for block in content:
			if not isinstance(block, dict):
				continue
			if block.get("type") in {"thinking", "redacted_thinking"}:
				return True

	return False


def _sanitize_request_controls(data: dict[str, Any], provider_name: str) -> None:
	if provider_name == "anthropic":
		return
	if not _request_has_claude_reasoning_artifacts(data):
		return

	for field in (
		"thinking",
		"reasoning",
		"reasoning_effort",
		"budget_tokens",
		"betas",
		"anthropic_version",
	):
		data.pop(field, None)


def _sanitize_thinking_blocks(thinking_blocks: Any, provider_name: str) -> Any:
	if not isinstance(thinking_blocks, list):
		return thinking_blocks

	if provider_name != "anthropic":
		return []

	sanitized_blocks: list[Any] = []
	for block in thinking_blocks:
		if not isinstance(block, dict):
			continue

		block_type = block.get("type")
		if block_type == "thinking":
			signature = block.get("signature")
			if not _is_non_empty_string(block.get("thinking")):
				continue
			if not _is_non_empty_string(signature):
				continue
		elif block_type == "text" and not _is_non_empty_string(block.get("text")):
			block = dict(block)
			block["text"] = _ANTHROPIC_EMPTY_TEXT_PLACEHOLDER

		sanitized_blocks.append(block)

	return sanitized_blocks


def _flatten_tool_result_content_for_zai(messages: Any, model_name: Any) -> None:
	"""Flatten OpenAI list-format tool_result content to a string for Z.ai/GLM.

	Upstream litellm#25868: Z.ai/GLM silently drops tool results when the
	message content is a list of {"type":"text"} blocks instead of a plain
	string. Claude Code emits Anthropic-style list content; this pre-emptive
	flattening keeps multi-turn MCP sessions working.
	"""
	if not isinstance(messages, list) or not isinstance(model_name, str):
		return
	lower_model = model_name.lower()
	if "glm" not in lower_model and "zai" not in lower_model:
		return
	for message in messages:
		if not isinstance(message, dict) or message.get("role") != "tool":
			continue
		content = message.get("content")
		if not isinstance(content, list):
			continue
		parts: list[str] = []
		for block in content:
			if isinstance(block, dict) and block.get("type") in {"text", "input_text"}:
				text = block.get("text")
				if isinstance(text, str):
					parts.append(text)
		if parts:
			message["content"] = "\n".join(parts)


def _sanitize_messages(messages: Any, provider_name: str) -> Any:
	if not isinstance(messages, list):
		return messages

	sanitized_messages: list[Any] = []
	for message in messages:
		if not isinstance(message, dict):
			sanitized_messages.append(message)
			continue

		updated_message = dict(message)
		if provider_name == "chatgpt" and updated_message.get("role") in {
			"system",
			"developer",
		}:
			updated_message["role"] = "user"

		content = updated_message.get("content")
		extracted_thinking = None
		has_tool_use = False
		if isinstance(content, list):
			for block in content:
				if not isinstance(block, dict):
					continue
				if block.get("type") == "thinking":
					extracted_thinking = block.get("thinking")
				elif block.get("type") == "tool_use":
					has_tool_use = True

		updated_message["content"] = _sanitize_content_blocks(
			content,
			provider_name,
		)

		if "thinking_blocks" in updated_message:
			sanitized_thinking_blocks = _sanitize_thinking_blocks(
				updated_message.get("thinking_blocks"),
				provider_name,
			)
			if sanitized_thinking_blocks:
				updated_message["thinking_blocks"] = sanitized_thinking_blocks
			else:
				updated_message.pop("thinking_blocks", None)

		if provider_name == "anthropic":
			# Anthropic rejects unknown fields on assistant messages echoed back
			# by Claude Code: 400 messages.N.reasoning_content: Extra inputs are
			# not permitted. Litellm mirrors thinking into reasoning_content
			# (and provider_specific_fields.reasoning_content) on responses; once
			# that leaks into the next request's history the API bounces it.
			updated_message.pop("reasoning_content", None)
			provider_specific_fields = updated_message.get("provider_specific_fields")
			if isinstance(provider_specific_fields, dict):
				provider_specific_fields = dict(provider_specific_fields)
				provider_specific_fields.pop("reasoning_content", None)
				provider_specific_fields.pop("thinking", None)
				provider_specific_fields.pop("thinking_blocks", None)
				if provider_specific_fields:
					updated_message["provider_specific_fields"] = provider_specific_fields
				else:
					updated_message.pop("provider_specific_fields", None)
		else:
			provider_specific_fields = updated_message.get("provider_specific_fields")
			provider_reasoning_content = None
			if isinstance(provider_specific_fields, dict):
				provider_specific_fields = dict(provider_specific_fields)
				provider_reasoning_content = provider_specific_fields.get(
					"reasoning_content"
				)
				provider_specific_fields.pop("reasoning_content", None)
				provider_specific_fields.pop("thinking", None)
				provider_specific_fields.pop("thinking_blocks", None)
				if provider_specific_fields:
					updated_message["provider_specific_fields"] = provider_specific_fields
				else:
					updated_message.pop("provider_specific_fields", None)

			if provider_name == "mistral":
				updated_message.pop("reasoning_content", None)
			else:
				reasoning_content = updated_message.get("reasoning_content")
				if not _is_non_empty_string(reasoning_content):
					if _is_non_empty_string(extracted_thinking):
						updated_message["reasoning_content"] = extracted_thinking
					elif _is_non_empty_string(provider_reasoning_content):
						updated_message["reasoning_content"] = provider_reasoning_content
					elif has_tool_use or updated_message.get("tool_calls"):
						updated_message["reasoning_content"] = " "
				elif reasoning_content == "":
					updated_message["reasoning_content"] = " "

		sanitized_messages.append(updated_message)

	return sanitized_messages


# vLLM-based backends (e.g. scaleway/qwen3.6) reject message arrays where a
# `system` or `developer` message appears after a user/assistant/tool turn. We
# canonicalise ordering by moving every system message to the front and merging
# their contents.

def _system_message_text(content: Any) -> str:
	if isinstance(content, str):
		return content
	if isinstance(content, list):
		parts = []
		for block in content:
			if isinstance(block, dict) and block.get("type") == "text":
				text = block.get("text")
				if isinstance(text, str):
					parts.append(text)
		return "\n".join(parts)
	return str(content)


def _normalize_system_messages_first(messages: Any) -> Any:
	if not isinstance(messages, list):
		return messages

	system_messages = []
	non_system_messages = []
	for message in messages:
		if isinstance(message, dict) and message.get("role") in {
			"system",
			"developer",
		}:
			system_messages.append(message)
		else:
			non_system_messages.append(message)
	if not system_messages:
		return messages

	merged_text = "\n\n".join(
		msg_text
		for msg_text in (
			_system_message_text(message.get("content"))
			for message in system_messages
		)
		if msg_text
	)
	return [{"role": "system", "content": merged_text}] + non_system_messages


def _clamp_provider_token_budget(data: dict[str, Any], provider_name: str) -> None:
	for token_field in ("max_completion_tokens", "max_tokens"):
		value = data.get(token_field)
		if not isinstance(value, (int, float)):
			continue
		if value <= 0:
			data[token_field] = 1024
			_TOOL_VALIDATION_LOGGER.warning(
				"Clamping invalid %s=%s to 1024 for provider=%s",
				token_field, value, provider_name
			)
		elif value > 32768:
			data[token_field] = 32768

	optional_params = data.get("optional_params")
	if isinstance(optional_params, dict):
		_clamp_provider_token_budget(optional_params, provider_name)


_MOONSHOT_DEEPSEEK_UNSUPPORTED_SCHEMA_KEYS = {
	"$defs",
	"$id",
	"$schema",
	"$ref",
	"allOf",
	"anyOf",
	"const",
	"definitions",
	"examples",
	"not",
	"oneOf",
}


def _sanitize_json_schema(schema: Any) -> Any:
	if not isinstance(schema, dict):
		return schema

	cleaned: dict[str, Any] = {}
	for key, value in schema.items():
		if key in _MOONSHOT_DEEPSEEK_UNSUPPORTED_SCHEMA_KEYS:
			continue

		if key in {"properties", "headers", "arguments", "input"}:
			if isinstance(value, dict):
				cleaned[key] = {
					prop_key: _sanitize_json_schema(prop_val)
					for prop_key, prop_val in value.items()
				}
				continue
		elif key == "items":
			cleaned[key] = (
				_sanitize_json_schema(value) if isinstance(value, dict) else value
			)
			continue

		if isinstance(value, dict):
			cleaned[key] = _sanitize_json_schema(value)
		elif isinstance(value, list):
			cleaned[key] = [_sanitize_json_schema(v) for v in value]
		else:
			cleaned[key] = value

	return cleaned


def _coerce_openai_tool(tool: Any, provider_name: str) -> dict[str, Any] | None:
	if not isinstance(tool, dict):
		return None

	if tool.get("type") == "function" and isinstance(tool.get("function"), dict):
		function = dict(tool["function"])
		if isinstance(function.get("function"), dict):
			function = dict(function["function"])
		name = function.get("name")
		if isinstance(name, str) and name.strip():
			function["name"] = name.strip()
			function.setdefault("parameters", {"type": "object", "properties": {}})
			if provider_name in {"moonshot", "deepseek"}:
				parameters = function.get("parameters")
				if isinstance(parameters, dict):
					function["parameters"] = _sanitize_json_schema(parameters)
			return {"type": "function", "function": function}
		return None

	# Responses-native tools can reach this late hook after a failed native
	# attempt is handed to a legacy fallback. Preserve their flat shape while
	# converting back to the Chat Completions tool contract used by fallbacks.
	if tool.get("type") == "function" and isinstance(tool.get("name"), str):
		name = tool["name"].strip()
		if not name:
			return None
		function = {
			"name": name,
			"parameters": tool.get("parameters")
				if isinstance(tool.get("parameters"), dict)
				else {"type": "object", "properties": {}},
		}
		for key in ("description", "strict"):
			if key in tool:
				function[key] = tool[key]
		return {"type": "function", "function": function}

	name = tool.get("name")
	if not isinstance(name, str) or not name.strip():
		return None
	name = name.strip()

	parameters = tool.get("input_schema")
	if not isinstance(parameters, dict):
		parameters = {"type": "object", "properties": {}}
	if provider_name in {"moonshot", "deepseek"}:
		parameters = _sanitize_json_schema(parameters)

	function = {"name": name, "parameters": parameters}
	description = tool.get("description")
	if isinstance(description, str):
		function["description"] = description
	return {"type": "function", "function": function}


def _sanitize_openai_tool_payload(data: dict[str, Any], provider_name: str) -> None:
	if provider_name == "anthropic":
		return

	tools = data.get("tools")
	valid_names: set[str] = set()
	if tools is None:
		data.pop("tools", None)
	elif isinstance(tools, list):
		sanitized_tools: list[dict[str, Any]] = []
		for tool in tools:
			coerced = _coerce_openai_tool(tool, provider_name)
			if coerced is None:
				continue
			sanitized_tools.append(coerced)
			valid_names.add(coerced["function"]["name"])
		if sanitized_tools:
			data["tools"] = sanitized_tools
		else:
			data.pop("tools", None)
	else:
		data.pop("tools", None)

	tool_choice = data.get("tool_choice")
	if tool_choice is None:
		data.pop("tool_choice", None)
	elif "tools" not in data:
		data.pop("tool_choice", None)
	elif isinstance(tool_choice, dict):
		function = tool_choice.get("function")
		name = function.get("name") if isinstance(function, dict) else None
		if tool_choice.get("type") != "function" or name not in valid_names:
			data.pop("tool_choice", None)
	elif isinstance(tool_choice, str):
		if tool_choice not in {"auto", "none", "required", "any"}:
			data.pop("tool_choice", None)
	else:
		data.pop("tool_choice", None)

	if "tools" not in data:
		data.pop("web_search_options", None)

	optional_params = data.get("optional_params")
	if isinstance(optional_params, dict):
		_sanitize_openai_tool_payload(optional_params, provider_name)


def _sanitize_anthropic_tool_payload(data: dict[str, Any]) -> None:
	tools = data.get("tools")
	if isinstance(tools, list):
		sanitized_tools = [
			tool
			for tool in tools
			if isinstance(tool, dict)
			and isinstance(tool.get("name"), str)
			and tool.get("name", "").strip()
		]
		if sanitized_tools:
			data["tools"] = sanitized_tools
		else:
			data.pop("tools", None)
	elif tools is not None:
		data.pop("tools", None)

	if "tools" not in data:
		data.pop("tool_choice", None)
		data.pop("web_search_options", None)

	optional_params = data.get("optional_params")
	if isinstance(optional_params, dict):
		_sanitize_anthropic_tool_payload(optional_params)


def _sanitize_responses_tool_payload(data: dict[str, Any]) -> None:
	tools = data.get("tools")
	if tools is None:
		data.pop("tools", None)
	elif isinstance(tools, list):
		sanitized_tools: list[dict[str, Any]] = []
		for tool in tools:
			if not isinstance(tool, dict):
				continue
			if tool.get("type") != "function":
				sanitized_tools.append(tool)
				continue

			function = tool.get("function")
			if not isinstance(function, dict):
				function = {}

			name = tool.get("name")
			if not isinstance(name, str) or not name.strip():
				name = function.get("name")
			if not isinstance(name, str) or not name.strip():
				continue

			flat_tool: dict[str, Any] = {
				"type": "function",
				"name": name.strip(),
			}

			description = function.get("description") or tool.get("description")
			if isinstance(description, str) and description.strip():
				flat_tool["description"] = description.strip()

			parameters = function.get("parameters") or tool.get("parameters")
			if isinstance(parameters, dict):
				flat_tool["parameters"] = parameters

			strict = function.get("strict")
			if strict is None:
				strict = tool.get("strict")
			if strict is not None:
				flat_tool["strict"] = strict

			sanitized_tools.append(flat_tool)

		if sanitized_tools:
			data["tools"] = sanitized_tools
		else:
			data.pop("tools", None)
	else:
		data.pop("tools", None)

	optional_params = data.get("optional_params")
	if isinstance(optional_params, dict):
		_sanitize_responses_tool_payload(optional_params)


def _request_has_tool_result(data: dict[str, Any]) -> bool:
	messages = data.get("messages")
	if not isinstance(messages, list):
		return False
	for message in messages:
		if not isinstance(message, dict):
			continue
		if message.get("role") == "tool":
			return True
		content = message.get("content")
		if isinstance(content, list):
			for block in content:
				if isinstance(block, dict) and block.get("type") == "tool_result":
					return True
	return False


def _nudge_single_tool_for_moonshot_and_deepseek(data: dict[str, Any], provider_name: str) -> None:
	if provider_name not in ("moonshot", "deepseek") or _request_has_tool_result(data):
		return
	targets = [data]
	optional_params = data.get("optional_params")
	if isinstance(optional_params, dict):
		targets.append(optional_params)

	for target in targets:
		tools = target.get("tools")
		if not isinstance(tools, list) or len(tools) != 1:
			continue
		tool = tools[0]
		if not isinstance(tool, dict):
			continue
		function = tool.get("function")
		if not isinstance(function, dict):
			continue
		name = function.get("name")
		if not isinstance(name, str) or not name.strip():
			continue
		tool_choice = target.get("tool_choice")
		if tool_choice in (None, "auto", "any", "required"):
			target["tool_choice"] = {"type": "function", "function": {"name": name}}


_ANTHROPIC_ONLY_BLOCK_TYPES = {
	"tool_use",
	"tool_result",
	"thinking",
	"redacted_thinking",
	"document",
}


def _request_is_anthropic_shape(data: dict[str, Any]) -> bool:
	"""Detect Anthropic Messages-API request shape regardless of provider label.

	Auto Router v2 aliases (`smart-router`, `smart-router-claude`) and the
	underlying `auto_router/complexity_router` model name are not resolvable
	by `get_llm_provider`, so the existing sanitizer bailed before stripping
	`reasoning_content`. When the inner tier is a Claude model the unstripped
	field hits Anthropic and bounces with
	`400 messages.N.reasoning_content: Extra inputs are not permitted`.
	"""
	if not isinstance(data, dict):
		return False
	if data.get("thinking") is not None or data.get("anthropic_version"):
		return True
	proxy_request = data.get("proxy_server_request")
	if isinstance(proxy_request, dict):
		url = str(proxy_request.get("url") or "")
		if "/v1/messages" in url:
			return True
	messages = data.get("messages")
	if not isinstance(messages, list):
		return False
	for message in messages:
		if not isinstance(message, dict):
			continue
		if message.get("thinking_blocks"):
			return True
		content = message.get("content")
		if not isinstance(content, list):
			continue
		for block in content:
			if not isinstance(block, dict):
				continue
			if block.get("type") in _ANTHROPIC_ONLY_BLOCK_TYPES:
				return True
			if block.get("type") == "image" and isinstance(block.get("source"), dict):
				return True
	return False


_RESPONSES_INPUT_ITEM_TYPES = frozenset(
	{
		"message",
		"function_call",
		"function_call_output",
		"custom_tool_call",
		"custom_tool_call_output",
		"local_shell_call",
		"local_shell_call_output",
		"web_search_call",
		"reasoning",
		"item_reference",
	}
)


def _request_is_responses_shape(data: dict[str, Any]) -> bool:
	"""Detect an OpenAI Responses-API request (Codex over LiteLLM /v1/responses).

	Marker is a top-level ``instructions`` string and/or an ``input`` list —
	neither of which the Anthropic Messages shape nor Chat Completions uses.
	Kept independent of provider label so the bridged path (glm/qwen/etc. via
	the /v1/responses -> /chat/completions bridge) is caught too.
	"""
	if not isinstance(data, dict):
		return False
	inp = data.get("input")
	if isinstance(data.get("instructions"), str) and "messages" not in data:
		return True
	# A bare `input` list matches only when every item is a Responses input
	# item: a message (has `role`) or a known typed item. Embeddings/moderation
	# send lists of strings, and multimodal embeddings send typed dicts like
	# `{"type": "image_url", ...}` — neither must match, or terse injection adds
	# an `instructions` field the provider rejects with a 400. A string `input`
	# without `instructions` is ambiguous with embeddings, so it is left out.
	if not isinstance(inp, list) or "messages" in data or not inp:
		return False
	return all(
		isinstance(item, dict)
		and ("role" in item or item.get("type") in _RESPONSES_INPUT_ITEM_TYPES)
		for item in inp
	)


def _response_repair_should_bypass(data: dict[str, Any] | None) -> bool:
	"""Skip Claude-shaped response tool-call repair for Responses-API traffic.

	The repair passes (native-text/planned-read/agent/command rewrites) assume
	an Anthropic `content` block list and tool names from Claude Code's own
	tool set. Codex sends Responses shape — native `chatgpt/*` (already bypassed)
	and bridged models (glm/qwen/etc. via /v1/responses). Running the Claude
	repairs on those is at best a structural no-op and at worst a mis-rewrite,
	so gate them off explicitly.
	"""
	if not isinstance(data, dict):
		return False
	if _is_chatgpt_native_responses_request(data):
		return True
	metadata = data.get("metadata")
	if isinstance(metadata, dict) and metadata.get("ccproxy_responses_shape"):
		return True
	return _request_is_responses_shape(data)


def _coerce_anthropic_provider_for_sanitization(
	data: dict[str, Any],
	routed_model: Any,
	provider_name: str | None,
) -> str:
	"""Force Anthropic-side sanitization when the request is Claude-shaped.

	For Auto Router aliases the outer routed model resolves to `auto_router`
	(or nothing), but the inner tier dispatch preserves `messages` verbatim
	and routes to Claude. `reasoning_content` / `provider_specific_fields`
	must be stripped as if the provider were Anthropic or Anthropic rejects
	the request. Coercion is safe for non-Claude tiers too: none of the
	configured SIMPLE/MEDIUM/COMPLEX tiers (gpt-oss, kimi, glm) consume
	`reasoning_content` on input.
	"""
	if provider_name == "anthropic":
		return "anthropic"
	if isinstance(routed_model, str) and (
		routed_model.startswith("smart-router")
		or routed_model.startswith("auto_router/")
	):
		return "anthropic"
	if provider_name in (None, "", "auto_router") and _request_is_anthropic_shape(data):
		return "anthropic"
	return provider_name or ""


def _provider_for_model_call(data: dict[str, Any]) -> str | None:
	routed_model = data.get("model") or ""
	if not isinstance(routed_model, str) or not routed_model:
		return None

	alias_map = _load_model_name_to_provider_map()
	provider_name = alias_map.get(routed_model)
	if provider_name:
		return _coerce_anthropic_provider_for_sanitization(
			data, routed_model, provider_name
		)

	try:
		_, provider_name, _, _ = get_llm_provider(
			model=routed_model,
			custom_llm_provider=data.get("custom_llm_provider"),
			api_base=data.get("api_base"),
		)
	except Exception:
		provider_name = None

	if not provider_name and routed_model.startswith("claude-"):
		provider_name = "anthropic"

	return _coerce_anthropic_provider_for_sanitization(
		data, routed_model, provider_name
	) or None


def _is_chatgpt_native_responses_request(data: dict[str, Any] | None) -> bool:
	"""Return true only for the provider-scoped ChatGPT Responses route.

	Do not use a bare ``gpt-5`` substring here: OpenAI API-key models such as
	``openai/gpt-5.6`` must not inherit ChatGPT OAuth or recovery behavior.
	"""
	if not isinstance(data, dict):
		return False
	provider = str(data.get("custom_llm_provider") or "").lower()
	model = str(data.get("model") or "").lower()
	chatgpt_models = {"gpt-5.6-terra", "gpt-5.6-sol", "gpt-5.6-luna", "gpt-5.5", "gpt-5.6"}
	if provider == "chatgpt" or model.startswith("chatgpt/") or model in chatgpt_models:
		return True
	metadata = data.get("metadata")
	if isinstance(metadata, dict):
		for key in ("ccproxy_provider_model", "ccproxy_litellm_model"):
			value = str(metadata.get(key) or "").lower()
			if value.startswith("chatgpt/") or value in chatgpt_models:
				return True
	return False


def _context_aware_max_tokens_clamp(data: dict[str, Any]) -> None:
	"""Reduce max_tokens/max_completion_tokens so prompt + output fits the
	model's declared context window. Fast char-based estimate (//4 chars per
	token, conservative 1024-token margin) so it is cheap to run on every
	request. Leaves the request untouched when the budget is unknown or the
	prompt already overflows (the backend or configured context_window_fallbacks
	handle that)."""
	messages = data.get("messages")
	if not isinstance(messages, list):
		return
	model_info = data.get("model_info")
	if not isinstance(model_info, dict):
		return
	context_window = model_info.get("context_window") or model_info.get("max_tokens")
	if not isinstance(context_window, (int, float)) or context_window < 1:
		return

	char_count = 0
	system = data.get("system")
	if isinstance(system, str):
		char_count += len(system)
	elif isinstance(system, list):
		for item in system:
			if isinstance(item, dict):
				char_count += len(str(item.get("text", "")))
	for msg in messages:
		if not isinstance(msg, dict):
			continue
		content = msg.get("content", "")
		if isinstance(content, str):
			char_count += len(content)
		elif isinstance(content, list):
			for item in content:
				if isinstance(item, dict):
					char_count += len(str(item.get("text", "")))
					if item.get("type") in {"image", "image_url"}:
						char_count += 4000
				elif isinstance(item, str):
					char_count += len(item)
	prompt_tokens = max(1, char_count // 4)
	available = int(context_window) - prompt_tokens - 1024
	if available < 1:
		return

	thinking_budget = 0
	thinking = data.get("thinking")
	if isinstance(thinking, dict) and thinking.get("type") == "enabled":
		budget = thinking.get("budget_tokens")
		if isinstance(budget, (int, float)) and not (
			isinstance(budget, float) and (math.isnan(budget) or math.isinf(budget))
		):
			budget_int = int(budget)
			if budget_int < 1024:
				data.pop("thinking", None)
			else:
				# Anthropic only uses max_tokens for the output ceiling.
				# Fall back to max_completion_tokens only if max_tokens is absent
				# so OpenAI-compatible clients still get a derived cap.
				requested_max = data.get("max_tokens") or 0
				created_max = False
				if requested_max <= 0:
					requested_max = data.get("max_completion_tokens") or 0
					if requested_max > 0:
						data["max_tokens"] = requested_max
						created_max = True
				if requested_max <= 0:
					# Anthropic requires max_tokens. Provide a cap derived from
					# the thinking budget so the request is not rejected outright.
					requested_max = min(available, budget_int + 1)
					if requested_max > 0:
						data["max_tokens"] = requested_max
						created_max = True
				if requested_max > 0:
					limit = min(available, requested_max)
					if budget_int >= limit:
						new_thinking_budget = max(1024, limit - 1)
						if new_thinking_budget >= limit:
							data.pop("thinking", None)
							if created_max:
								data.pop("max_tokens", None)
							_TOOL_VALIDATION_LOGGER.info(
								"context clamp: model=%s disabled thinking (budget=%s limit=%s)",
								data.get("model"), budget_int, limit,
							)
						else:
							thinking["budget_tokens"] = new_thinking_budget
							thinking_budget = new_thinking_budget
							_TOOL_VALIDATION_LOGGER.info(
								"context clamp: model=%s reduced thinking budget -> %s (limit=%s)",
								data.get("model"), thinking_budget, limit,
							)
		else:
			# NaN, infinity, or non-numeric budget_tokens is invalid; remove it.
			data.pop("thinking", None)

	min_max_tokens = thinking_budget + 1 if thinking_budget > 0 else 1
	model = data.get("model")
	for field in ("max_tokens", "max_completion_tokens"):
		val = data.get(field)
		if isinstance(val, (int, float)) and val > available:
			clamped = max(min_max_tokens, available)
			_TOOL_VALIDATION_LOGGER.info(
				"context clamp: model=%s %s=%s -> %s (prompt~%s thinking=%s ctx=%s)",
				model, field, val, clamped, prompt_tokens, thinking_budget, int(context_window),
			)
			data[field] = clamped

	# Some providers (e.g. scaleway/glm-5.2) reject max_tokens/max_completion_tokens
	# above a hard ceiling even when the context window has room. Apply a global cap
	# so requests don't bounce with a 400. Anthropic-format clients send
	# `max_tokens`; litellm's OpenAI adapter converts it to `max_completion_tokens`
	# AFTER this clamp, so we must cap BOTH fields to be safe.
	try:
		import litellm as _litellm_cap_lookup
	except Exception:
		_litellm_cap_lookup = None

	_provider_output_cap = None
	_model_cost = getattr(_litellm_cap_lookup, "model_cost", {}) if _litellm_cap_lookup else {}
	if isinstance(_model_cost, dict):
		model_name_lower = str(model or "").lower()
		_candidate_names: list[str] = [str(model or "")]
		if "/" in model_name_lower:
			_stripped = model_name_lower.split("/", 1)[1]
			_candidate_names.append(_stripped)
			for _provider_prefix in (
				"openai",
				"zai",
				"chatgpt",
				"anthropic",
				"moonshot",
				"deepseek",
				"mistral",
				"minimax",
				"qwencloud",
				"qwencloud-payg",
				"scaleway",
				"cf",
				"or",
			):
				_candidate_names.append(f"{_provider_prefix}/{_stripped}")
		for _candidate in _candidate_names:
			_entry = _model_cost.get(_candidate)
			if not isinstance(_entry, dict):
				continue
			_max_out = _entry.get("max_output_tokens")
			if isinstance(_max_out, (int, float)) and _max_out > 0:
				_provider_output_cap = int(_max_out)
				break

	if _provider_output_cap is None:
		# When the request already carries an explicit Anthropic output ceiling,
		# use it as the only safe cap for the parallel OpenAI field. Do not apply a
		# guessed global cap to a model whose capability metadata is unavailable.
		_requested_max = data.get("max_tokens")
		if isinstance(_requested_max, (int, float)) and _requested_max > 0:
			_provider_output_cap = int(_requested_max)

	for _token_field in ("max_completion_tokens", "max_tokens"):
		_val = data.get(_token_field)
		if not isinstance(_val, (int, float)):
			continue
		if _provider_output_cap is not None and _val > _provider_output_cap:
			_TOOL_VALIDATION_LOGGER.info(
				"output cap: model=%s %s=%s -> %s (provider max_output_tokens)",
				model, _token_field, _val, _provider_output_cap,
			)
			data[_token_field] = _provider_output_cap


def _sanitize_model_call_payload(data: dict[str, Any], provider_name: str) -> dict[str, Any]:
	_sanitize_request_controls(data, provider_name)
	native_chatgpt = provider_name == "chatgpt" or _is_chatgpt_native_responses_request(data)
	# Aggressive safety clamp for max_tokens before any processing
	for token_field in ("max_completion_tokens", "max_tokens"):
		if token_field in data:
			value = data[token_field]
			if isinstance(value, (int, float)) and value < 1:
				data[token_field] = 1024
				_TOOL_VALIDATION_LOGGER.warning(
					"Safety: clamping %s=%s to 1024 before processing", token_field, value
				)
	if isinstance(data.get("messages"), list) and not native_chatgpt:
		_flatten_tool_result_content_for_zai(data.get("messages"), data.get("model"))
		data["messages"] = _sanitize_messages(data.get("messages"), provider_name)
	if provider_name != "anthropic" and not native_chatgpt and isinstance(data.get("messages"), list):
		data["messages"] = _normalize_system_messages_first(data.get("messages"))
	_clamp_provider_token_budget(data, provider_name)
	if provider_name == "anthropic":
		_sanitize_anthropic_tool_payload(data)
	# ChatGPT's /v1/messages bridge owns the Anthropic -> Responses conversion.
	# Preserve Anthropic `name`/`input_schema` tools and `tool_choice` here; the
	# generic OpenAI sanitizer would convert them to nested function tools and
	# then discard the Anthropic `{"type":"tool","name":...}` choice.
	if not native_chatgpt:
		_sanitize_openai_tool_payload(data, provider_name)
	_nudge_single_tool_for_moonshot_and_deepseek(data, provider_name)
	if isinstance(data.get("messages"), list) and not native_chatgpt:
		required_map = _required_fields_by_tool(data)
		messages, patched = _sanitize_history_tool_calls(
			data.get("messages"), required_map
		)
		data["messages"] = messages
		if patched:
			_TOOL_VALIDATION_LOGGER.info(
				"request history tool-call validation: patched=%d model=%s provider=%s",
				patched,
				data.get("model"),
			provider_name,
		)
	_context_aware_max_tokens_clamp(data)
	return data


# ---------------------------------------------------------------------------
# Tool-call validation
#
# Non-Anthropic models routed through the gateway (mistral, kimi, glm-5.1,
# scaleway-devstral, ...) frequently emit malformed tool calls when handling
# Claude Code's strict tool schemas -- missing required arguments,
# empty-object `input`, or unparseable JSON. Claude Code then surfaces
# `tool_use_error: required parameter X missing`, the model sees that error
# in the next turn, and tries the same broken call again, looping forever.
#
# To break the loop we run two passes:
#   1. Request-side history cleanup (`_sanitize_history_tool_calls`):
#      replace prior malformed `assistant.tool_use` blocks with a text note,
#      drop the matching `user.tool_result` blocks. The model sees a clean
#      history instead of its own broken pattern.
#   2. Response-side validation (`_sanitize_response_tool_calls`, wired via
#      `_ValidatingCCProxyHandler.async_post_call_success_hook`): catch the
#      bad tool call before the agent acts on it, replace with a text block
#      explaining what was missing.
# ---------------------------------------------------------------------------

_TOOL_VALIDATION_LOGGER = logging.getLogger("ccproxy.tool_validation")
# LiteLLM leaves the root logger at WARNING unless LITELLM_LOG is set, which
# swallows every ccproxy INFO line (reroutes, cache usage, sanitiser actions).
# Give this logger its own stderr handler so observability never depends on
# LiteLLM's global verbosity.
if not _TOOL_VALIDATION_LOGGER.handlers:
	_ccproxy_log_handler = logging.StreamHandler()
	_ccproxy_log_handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
	_TOOL_VALIDATION_LOGGER.addHandler(_ccproxy_log_handler)
	_TOOL_VALIDATION_LOGGER.propagate = False
_TOOL_VALIDATION_LOGGER.setLevel(
	logging.DEBUG if os.getenv("CCPROXY_DEBUG", "").strip().lower() in {"1", "true", "yes", "on"} else logging.INFO
)
_THINK_TAG_RE = re.compile(r"(?is)<think\b[^>]*>.*?</think\s*>")
_THINK_LINE_RE = re.compile(r"(?im)^\s*</?think\b[^>]*>\s*$")
_KIMI_TOOL_CALL_MARKER_RE = re.compile(
	r"(?is)<tool_call_begin>\s*functions\.([A-Za-z0-9_.:-]+)\s*:\s*(\d+)\s*<tool_call_end>"
)
_BASH_FENCE_RE = re.compile(r"(?is)^\s*```(?:bash|sh|shell)?\s*\n(.+?)\n```\s*$")
_BASH_EXPLICIT_FENCE_RE = re.compile(r"(?is)```(?:bash|sh|shell)\s*\n(.*?)\n```")
_BASH_GENERIC_FENCE_RE = re.compile(r"(?is)```\s*\n(.*?)\n```")
_DEEPSEEK_DSML_PREFIX = r"(?:｜｜|\|\|)DSML(?:｜｜|\|\|)"
_DEEPSEEK_DSML_INVOKE_RE = re.compile(
	rf"(?is)<{_DEEPSEEK_DSML_PREFIX}invoke\s+name=[\"']([^\"']+)[\"'][^>]*>(.*?)</{_DEEPSEEK_DSML_PREFIX}invoke>"
)
_DEEPSEEK_DSML_PARAM_RE = re.compile(
	rf"(?is)<{_DEEPSEEK_DSML_PREFIX}parameter\s+name=[\"']([^\"']+)[\"'][^>]*>(.*?)</{_DEEPSEEK_DSML_PREFIX}parameter>"
)
_BASH_XML_RE = re.compile(r"(?is)<bash>\s*(.*?)\s*</bash>")
_GENERIC_TOOL_CALL_RE = re.compile(
	r"(?is)<tool_call\s+name=[\"']([^\"']+)[\"'][^>]*>(.*?)</tool_call>"
)
_GENERIC_TOOL_PARAM_RE = re.compile(
	r"(?is)<parameter\s+name=[\"']([^\"']+)[\"'][^>]*>(.*?)</parameter>"
)
_FILE_READ_TAG_RE = re.compile(r"(?is)<file-read\b([^>]*)/?>")
_READ_FILE_BLOCK_RE = re.compile(r"(?is)<read_file\b[^>]*>(.*?)</read_file>")
_READ_FILE_PATH_RE = re.compile(
	r"(?is)<(?:path|file_path)\b[^>]*>(.*?)</(?:path|file_path)>"
)
_BASH_XML_COMMAND_RE = re.compile(r"(?is)<command\b[^>]*>(.*?)</command>")
_XML_ATTR_RE = re.compile(r"(?is)\b([A-Za-z_][A-Za-z0-9_.:-]*)=[\"']([^\"']*)[\"']")
_CODEX_COMMAND_KEYS = ("cmd", "command")

# Z.ai/GLM sometimes emits raw OpenAI-style tool_calls JSON or <invoke> XML
# instead of native tool_calls when complex MCP schemas confuse the model.
_ZAI_INVOKE_RE = re.compile(r"(?is)<invoke\s+name=[\"']([^\"']+)[\"'][^>]*>(.*?)</invoke>")
_ZAI_PARAMETER_RE = re.compile(r"(?is)<parameter\s+name=[\"']([^\"']+)[\"'][^>]*>(.*?)</parameter>")


def _required_fields_by_tool(data: dict[str, Any]) -> dict[str, list[str]]:
	"""Build `{tool_name: [required_field, ...]}` from request tool schemas."""
	required_map: dict[str, list[str]] = {}
	tools = data.get("tools")
	if not isinstance(tools, list):
		return required_map

	for tool in tools:
		if not isinstance(tool, dict):
			continue

		# OpenAI format: {"type": "function", "function": {"name": ..., "parameters": {...}}}
		name: Any = None
		schema: Any = None
		if tool.get("type") == "function" and isinstance(tool.get("function"), dict):
			function_def = tool["function"]
			name = function_def.get("name")
			schema = function_def.get("parameters")
		else:
			# Anthropic format: {"name": ..., "input_schema": {...}}
			name = tool.get("name")
			schema = tool.get("input_schema")

		if not isinstance(name, str) or not isinstance(schema, dict):
			continue

		required = schema.get("required")
		if not isinstance(required, list):
			continue

		required_map[name] = [field for field in required if isinstance(field, str)]

	return required_map


def _coerce_tool_input(raw_input: Any) -> tuple[dict[str, Any] | None, bool]:
	"""Return `(parsed_dict, parse_ok)`. Strings get JSON-decoded."""
	if isinstance(raw_input, dict):
		return raw_input, True
	if isinstance(raw_input, str):
		stripped = raw_input.strip()
		if not stripped:
			return {}, True
		try:
			import json

			parsed = json.loads(stripped)
		except Exception:
			return None, False
		if isinstance(parsed, dict):
			return parsed, True
		return None, False
	if raw_input is None:
		return {}, True
	return None, False


def _missing_required_fields(
	tool_name: str,
	tool_input: dict[str, Any],
	required_map: dict[str, list[str]],
) -> list[str]:
	required = required_map.get(tool_name)
	if not required:
		return []
	missing: list[str] = []
	for field in required:
		value = tool_input.get(field)
		if value is None:
			missing.append(field)
			continue
		if isinstance(value, str) and not value.strip():
			missing.append(field)
	return missing


def _normalize_read_path_value(value: str) -> str:
	"""Remove unmatched shell-quote artifacts from generated Read paths."""
	path = value.strip()
	while (
		len(path) > 1
		and path[-1] in {"'", '"', "`"}
		and not path.startswith(path[-1])
	):
		path = path[:-1].rstrip()
	while (
		len(path) > 1
		and path[0] in {"'", '"', "`"}
		and not path.endswith(path[0])
	):
		path = path[1:].lstrip()
	if (
		len(path) > 1
		and path[0] == path[-1]
		and path[0] in {"'", '"', "`"}
	):
		path = path[1:-1].strip()
	return path


def _normalize_read_tool_input(tool_name: Any, tool_input: Any) -> bool:
	"""Mutate Read tool input in place. Return True when changed."""
	if tool_name != "Read" or not isinstance(tool_input, dict):
		return False
	changed = False
	for field in ("file_path", "path"):
		value = tool_input.get(field)
		if not isinstance(value, str):
			continue
		normalized = _normalize_read_path_value(value)
		if normalized and normalized != value:
			tool_input[field] = normalized
			changed = True
	return changed


def _malformed_tool_text(tool_name: str, missing: list[str], parse_failed: bool) -> str:
	if parse_failed:
		return (
			f"[gateway] Dropped malformed call to tool `{tool_name}`: arguments "
			"were not valid JSON. Retry with a complete arguments object."
		)
	if missing:
		fields = ", ".join(f"`{field}`" for field in missing)
		return (
			f"[gateway] Dropped malformed call to tool `{tool_name}`: missing "
			f"required field(s) {fields}. Retry with all required arguments."
		)
	return (
		f"[gateway] Dropped malformed call to tool `{tool_name}`. Retry with a "
		"complete arguments object."
	)


def _sanitize_history_tool_calls(
	messages: Any,
	required_map: dict[str, list[str]],
) -> tuple[Any, int]:
	"""Rewrite malformed `assistant.tool_use` blocks in conversation history.

	Returns `(new_messages, count_patched)`.

	Patched blocks become `{"type": "text", "text": "[gateway] ..."}`. Matching
	`user.tool_result` blocks (referenced by `tool_use_id`) are dropped so the
	model is not re-fed its own bad tool/result pair.
	"""
	if not isinstance(messages, list) or not required_map:
		return messages, 0

	dropped_ids: set[str] = set()
	patched = 0
	new_messages: list[Any] = []

	for message in messages:
		if not isinstance(message, dict):
			new_messages.append(message)
			continue

		content = message.get("content")
		if not isinstance(content, list):
			new_messages.append(message)
			continue

		role = message.get("role")
		new_content: list[Any] = []

		for block in content:
			if not isinstance(block, dict):
				new_content.append(block)
				continue

			block_type = block.get("type")

			# Drop tool_result blocks that referenced a patched tool_use
			if (
				role == "user"
				and block_type == "tool_result"
				and block.get("tool_use_id") in dropped_ids
			):
				continue

			# Validate assistant tool_use
			if role == "assistant" and block_type == "tool_use":
				tool_name = block.get("name")
				parsed, parse_ok = _coerce_tool_input(block.get("input"))
				if not isinstance(tool_name, str):
					new_content.append(block)
					continue
				missing: list[str] = []
				if parse_ok and isinstance(parsed, dict):
					if _normalize_read_tool_input(tool_name, parsed):
						block["input"] = parsed
						patched += 1
					missing = _missing_required_fields(tool_name, parsed, required_map)
				if (not parse_ok) or missing:
					tool_id = block.get("id")
					if isinstance(tool_id, str):
						dropped_ids.add(tool_id)
					new_content.append(
						{
							"type": "text",
							"text": _malformed_tool_text(tool_name, missing, not parse_ok),
						}
					)
					patched += 1
					continue

			new_content.append(block)

		if new_content:
			updated_message = dict(message)
			updated_message["content"] = new_content
			new_messages.append(updated_message)
		else:
			# An assistant message with only a malformed tool_use becomes a single
			# text block; preserve message presence so role ordering stays valid.
			updated_message = dict(message)
			updated_message["content"] = [
				{"type": "text", "text": "[gateway] (previous tool call was dropped)"}
			]
			new_messages.append(updated_message)

	return new_messages, patched


def _validate_openai_tool_call(
	tool_call: Any, required_map: dict[str, list[str]]
) -> bool:
	"""Mutate a single OpenAI-format tool call in place. Return True if patched."""
	import json

	try:
		function = (
			getattr(tool_call, "function", None)
			if not isinstance(tool_call, dict)
			else tool_call.get("function")
		)
	except Exception:
		return False
	if function is None:
		return False

	name = (
		getattr(function, "name", None)
		if not isinstance(function, dict)
		else function.get("name")
	)
	arguments = (
		getattr(function, "arguments", None)
		if not isinstance(function, dict)
		else function.get("arguments")
	)
	if not isinstance(name, str):
		return False

	parsed, parse_ok = _coerce_tool_input(arguments)
	missing: list[str] = []
	if parse_ok and isinstance(parsed, dict):
		missing = _missing_required_fields(name, parsed, required_map)
	if parse_ok and not missing:
		return False

	# Patch: keep the schema valid by injecting empty placeholders for missing
	# required fields and append a sentinel marker so the agent layer can spot
	# and short-circuit. We can't safely drop a tool_call from a streamed
	# response shape, so coercing to a runnable-but-flagged call is the
	# pragmatic compromise.
	fixed: dict[str, Any] = parsed if (parse_ok and isinstance(parsed, dict)) else {}
	for field in missing:
		fixed[field] = ""
	fixed["_gateway_validation_error"] = _malformed_tool_text(name, missing, not parse_ok)
	new_arguments = json.dumps(fixed, ensure_ascii=False)
	if isinstance(function, dict):
		function["arguments"] = new_arguments
	else:
		try:
			function.arguments = new_arguments
		except Exception:
			return False
	return True


def _sanitize_response_tool_calls(
	response: Any, required_map: dict[str, list[str]]
) -> int:
	"""Walk both Anthropic and OpenAI response shapes, patch malformed calls."""
	if not required_map or response is None:
		return 0

	patched = 0
	dropped_malformed = 0

	# Anthropic-style response: `.content` (or response["content"]) is a list
	# of blocks; tool_use blocks live there alongside text blocks.
	content: Any = None
	try:
		if isinstance(response, dict):
			content = response.get("content")
		else:
			content = getattr(response, "content", None)
	except Exception:
		content = None

	if isinstance(content, list):
		new_content: list[Any] = []
		for block in content:
			if isinstance(block, dict) and block.get("type") == "tool_use":
				tool_name = block.get("name")
				parsed, parse_ok = _coerce_tool_input(block.get("input"))
				missing: list[str] = []
				if parse_ok and isinstance(parsed, dict) and isinstance(tool_name, str):
					if _normalize_read_tool_input(tool_name, parsed):
						block["input"] = parsed
						patched += 1
					missing = _missing_required_fields(
						tool_name, parsed, required_map
					)
				if isinstance(tool_name, str) and ((not parse_ok) or missing):
					new_content.append(
						{
							"type": "text",
							"text": _malformed_tool_text(
								tool_name, missing, not parse_ok
							),
						}
					)
					patched += 1
					dropped_malformed += 1
					continue
				new_content.append(block)
			else:
				new_content.append(block)

		if dropped_malformed and not any(
			isinstance(b, dict) and b.get("type") == "text" for b in new_content
		):
			new_content.append(
				{
					"type": "text",
					"text": "[gateway] All tool calls in this response were malformed and dropped.",
				}
			)

		if isinstance(response, dict):
			response["content"] = new_content
		else:
			try:
				response.content = new_content
			except Exception:
				pass

	# OpenAI-style response: `.choices[*].message.tool_calls[*]`
	try:
		if isinstance(response, dict):
			choices = response.get("choices")
		else:
			choices = getattr(response, "choices", None)
	except Exception:
		choices = None

	if isinstance(choices, list):
		for choice in choices:
			try:
				if isinstance(choice, dict):
					message = choice.get("message")
				else:
					message = getattr(choice, "message", None)
			except Exception:
				message = None
			if message is None:
				continue
			try:
				if isinstance(message, dict):
					tool_calls = message.get("tool_calls")
				else:
					tool_calls = getattr(message, "tool_calls", None)
			except Exception:
				tool_calls = None
			if not isinstance(tool_calls, list):
				continue
			for tool_call in tool_calls:
				if _validate_openai_tool_call(tool_call, required_map):
					patched += 1

	return patched


def _strip_think_tag_artifacts(value: str) -> tuple[str, bool]:
	"""Remove model-native think tags that leak as visible assistant text."""
	if "<think" not in value.lower() and "</think" not in value.lower():
		return value, False

	updated = _THINK_TAG_RE.sub("", value)
	updated = _THINK_LINE_RE.sub("", updated)
	updated = "\n".join(line for line in updated.splitlines() if line.strip())
	if not updated.strip():
		updated = _ANTHROPIC_EMPTY_TEXT_PLACEHOLDER
	return updated, updated != value


def _extract_kimi_native_tool_calls(text: str) -> list[dict[str, Any]]:
	"""Parse Kimi native `<tool_call_begin>functions.X` text into tool_use blocks."""
	if "<tool_call_begin>" not in text:
		return []

	import json

	decoder = json.JSONDecoder()
	blocks: list[dict[str, Any]] = []
	for match in _KIMI_TOOL_CALL_MARKER_RE.finditer(text):
		tool_name = match.group(1)
		position = match.end()
		while position < len(text) and text[position].isspace():
			position += 1
		try:
			arguments, _ = decoder.raw_decode(text, position)
		except Exception:
			continue
		if not isinstance(arguments, dict):
			continue
		blocks.append(
			{
				"type": "tool_use",
				"id": f"toolu_kimi_{int(time.time() * 1000)}_{len(blocks)}",
				"name": tool_name,
				"input": arguments,
			}
		)
	return blocks


def _maybe_json_parameter(value: str) -> Any:
	stripped = html.unescape(value).strip()
	if not stripped:
		return ""
	if stripped[0] not in "{[":
		return stripped
	try:
		parsed = json.loads(stripped)
	except Exception:
		return stripped
	return parsed


def _available_tool_names(data: dict[str, Any] | None) -> set[str]:
	if not isinstance(data, dict):
		return set()

	tools = data.get("tools")
	if not isinstance(tools, list):
		return set()

	names: set[str] = set()
	for tool in tools:
		if not isinstance(tool, dict):
			continue
		name = None
		if tool.get("type") == "function" and isinstance(tool.get("function"), dict):
			name = tool["function"].get("name")
		else:
			name = tool.get("name")
		if isinstance(name, str) and name.strip():
			names.add(name.strip())
	return names


def _tool_is_available(data: dict[str, Any] | None, tool_name: str) -> bool:
	return tool_name in _available_tool_names(data)


def _canonical_text_tool_name(tool_name: str, available_tools: set[str]) -> str | None:
	if tool_name in available_tools:
		return tool_name
	aliases = {
		"Task": "Agent",
		"Agent": "Task",
	}
	alias = aliases.get(tool_name)
	if alias in available_tools:
		return alias
	return None


def _json_object_from_text(text: str) -> dict[str, Any] | None:
	decoder = json.JSONDecoder()
	position = 0
	while position < len(text):
		start = text.find("{", position)
		if start < 0:
			return None
		try:
			value, _ = decoder.raw_decode(text, start)
		except json.JSONDecodeError:
			position = start + 1
			continue
		if isinstance(value, dict):
			return value
		position = start + 1
	return None


def _single_required_field_tool(
	data: dict[str, Any] | None,
	tool_name: str,
) -> str | None:
	if not isinstance(data, dict):
		return None
	required = _required_fields_by_tool(data).get(tool_name)
	if isinstance(required, list) and len(required) == 1:
		return required[0]
	return None


def _extract_generic_xml_tool_calls(
	text: str,
	data: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
	if "<tool_call" not in text.lower():
		return []

	available_tools = _available_tool_names(data)
	if not available_tools:
		return []

	blocks: list[dict[str, Any]] = []
	for match in _GENERIC_TOOL_CALL_RE.finditer(text):
		tool_name = html.unescape(match.group(1)).strip()
		if tool_name not in available_tools:
			continue

		body = match.group(2).strip()
		arguments = _json_object_from_text(html.unescape(body))
		if arguments is None:
			arguments = {}
			for param_match in _GENERIC_TOOL_PARAM_RE.finditer(body):
				param_name = html.unescape(param_match.group(1)).strip()
				if not param_name:
					continue
				arguments[param_name] = _maybe_json_parameter(param_match.group(2))
		if not arguments:
			field_name = _single_required_field_tool(data, tool_name)
			if field_name:
				raw_body = html.unescape(re.sub(r"(?is)<[^>]+>", "", body)).strip()
				if raw_body:
					arguments = {field_name: raw_body}
		if not isinstance(arguments, dict) or not arguments:
			continue

		blocks.append(
			{
				"type": "tool_use",
				"id": f"toolu_xml_{int(time.time() * 1000)}_{len(blocks)}",
				"name": tool_name,
				"input": arguments,
			}
		)
	return blocks


def _extract_file_read_tag_tool_calls(
	text: str,
	data: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
	if "<file-read" not in text.lower() or not _tool_is_available(data, "Read"):
		return []

	read_config = _read_tool_config(data or {})
	if read_config is None:
		return []
	tool_name, path_field = read_config
	blocks: list[dict[str, Any]] = []
	for match in _FILE_READ_TAG_RE.finditer(text):
		attrs = {
			html.unescape(name).strip(): html.unescape(value).strip()
			for name, value in _XML_ATTR_RE.findall(match.group(1))
		}
		file_path = attrs.get("file_path") or attrs.get("path")
		if not file_path:
			continue
		blocks.append(
			{
				"type": "tool_use",
				"id": f"toolu_file_read_{int(time.time() * 1000)}_{len(blocks)}",
				"name": tool_name,
				"input": {path_field: file_path},
			}
		)
	return blocks


def _extract_read_file_block_tool_calls(
	text: str,
	data: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
	if "<read_file" not in text.lower() or not _tool_is_available(data, "Read"):
		return []

	read_config = _read_tool_config(data or {})
	if read_config is None:
		return []
	tool_name, path_field = read_config
	blocks: list[dict[str, Any]] = []
	for match in _READ_FILE_BLOCK_RE.finditer(text):
		body = match.group(1)
		path_match = _READ_FILE_PATH_RE.search(body)
		if path_match is None:
			continue
		file_path = html.unescape(re.sub(r"(?is)<[^>]+>", "", path_match.group(1))).strip()
		if not file_path:
			continue
		blocks.append(
			{
				"type": "tool_use",
				"id": f"toolu_read_file_{int(time.time() * 1000)}_{len(blocks)}",
				"name": tool_name,
				"input": {path_field: file_path},
			}
		)
	return blocks


def _extract_function_style_tool_calls(
	text: str,
	data: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
	available_tools = _available_tool_names(data)
	if not available_tools or "({" not in text:
		return []

	blocks: list[dict[str, Any]] = []
	decoder = json.JSONDecoder()
	for tool_name in sorted(available_tools, key=len, reverse=True):
		marker = f"{tool_name}("
		position = 0
		while position < len(text):
			start = text.find(marker, position)
			if start < 0:
				break
			json_start = start + len(marker)
			try:
				arguments, json_end = decoder.raw_decode(text, json_start)
			except json.JSONDecodeError:
				position = start + len(marker)
				continue
			trailer = text[json_end:].lstrip()
			if not trailer.startswith(")") or not isinstance(arguments, dict):
				position = json_end
				continue
			blocks.append(
				{
					"type": "tool_use",
					"id": f"toolu_func_{int(time.time() * 1000)}_{len(blocks)}",
					"name": tool_name,
					"input": arguments,
				}
			)
			position = json_end + (len(text[json_end:]) - len(trailer)) + 1
	return blocks


def _extract_deepseek_dsml_tool_calls(text: str) -> list[dict[str, Any]]:
	if "DSML" not in text:
		return []

	blocks: list[dict[str, Any]] = []
	for match in _DEEPSEEK_DSML_INVOKE_RE.finditer(text):
		tool_name = html.unescape(match.group(1)).strip()
		if not tool_name:
			continue
		body = match.group(2)
		arguments: dict[str, Any] = {}
		for param_match in _DEEPSEEK_DSML_PARAM_RE.finditer(body):
			param_name = html.unescape(param_match.group(1)).strip()
			if not param_name:
				continue
			arguments[param_name] = _maybe_json_parameter(param_match.group(2))
		blocks.append(
			{
				"type": "tool_use",
				"id": f"toolu_dsml_{int(time.time() * 1000)}_{len(blocks)}",
				"name": tool_name,
				"input": arguments,
			}
		)
	return blocks


def _extract_bash_xml_tool_calls(
	text: str,
	data: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
	if "<bash" not in text.lower():
		return []

	tool_config = _single_required_command_tool(data or {})
	if not tool_config:
		return []
	tool_name, command_field = tool_config

	blocks: list[dict[str, Any]] = []
	for match in _BASH_XML_RE.finditer(text):
		body = match.group(1)
		command_match = _BASH_XML_COMMAND_RE.search(body)
		if command_match is not None:
			command = html.unescape(command_match.group(1)).strip()
		else:
			command = html.unescape(re.sub(r"(?is)<[^>]+>", "", body)).strip()
		if not command:
			continue
		blocks.append(
			{
				"type": "tool_use",
				"id": f"toolu_bash_xml_{int(time.time() * 1000)}_{len(blocks)}",
				"name": tool_name,
				"input": {command_field: command},
			}
		)
	return blocks


def _looks_like_codex_shell_command(text: str) -> bool:
	stripped = text.strip()
	if not stripped or len(stripped) > 4000 or "\x00" in stripped:
		return False
	first_line = next((line.strip() for line in stripped.splitlines() if line.strip()), "")
	if not first_line:
		return False
	first_token = re.split(r"[\s;|&]+", first_line, 1)[0]
	first_token = first_token.strip()
	if not first_token:
		return False
	common_commands = {
		"awk",
		"bash",
		"cat",
		"cd",
		"chmod",
		"cp",
		"curl",
		"date",
		"docker",
		"echo",
		"env",
		"find",
		"git",
		"grep",
		"head",
		"jq",
		"ls",
		"make",
		"mkdir",
		"mv",
		"node",
		"npm",
		"printf",
		"python",
		"python3",
		"rg",
		"rm",
		"rmdir",
		"sed",
		"sh",
		"sort",
		"tail",
		"tee",
		"test",
		"touch",
		"true",
		"wc",
	}
	return first_token.startswith(("./", "/")) or first_token in common_commands


def _iter_json_objects_in_text(text: str):
	decoder = json.JSONDecoder()
	position = 0
	while position < len(text):
		start = text.find("{", position)
		if start < 0:
			return
		try:
			value, end = decoder.raw_decode(text, start)
		except json.JSONDecodeError:
			position = start + 1
			continue
		if isinstance(value, dict):
			yield value
		position = end


def _command_from_codex_object(obj: dict[str, Any]) -> str | None:
	command = None
	for key in _CODEX_COMMAND_KEYS:
		value = obj.get(key)
		if isinstance(value, str) and value.strip():
			command = value.strip()
			break
	if not command or not _looks_like_codex_shell_command(command):
		return None
	return command


def _extract_codex_json_command_tool_calls(
	text: str,
	data: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
	if "\"cmd\"" not in text and "\"command\"" not in text:
		return []

	tool_config = _single_required_command_tool(data or {})
	if not tool_config:
		return []
	tool_name, command_field = tool_config

	blocks: list[dict[str, Any]] = []
	for obj in _iter_json_objects_in_text(text):
		command = _command_from_codex_object(obj)
		if not command:
			continue
		blocks.append(
			{
				"type": "tool_use",
				"id": f"toolu_codex_cmd_{int(time.time() * 1000)}_{len(blocks)}",
				"name": tool_name,
				"input": {command_field: command},
			}
		)
	return blocks


def _extract_json_tool_intent_calls(
	text: str,
	data: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
	"""Parse model-written JSON tool intents into Anthropic tool_use blocks."""
	if '"tool"' not in text:
		return []

	available_tools = _available_tool_names(data)
	if not available_tools:
		return []
	required_map = _required_fields_by_tool(data or {})

	blocks: list[dict[str, Any]] = []
	for obj in _iter_json_objects_in_text(text):
		tool_name = obj.get("tool")
		if not isinstance(tool_name, str):
			continue
		tool_name = tool_name.strip()
		tool_name = _canonical_text_tool_name(tool_name, available_tools)
		if tool_name is None:
			continue

		if "arguments" in obj:
			raw_input = obj.get("arguments")
		else:
			# Some Claude/Haiku turns emit {"tool":"Task", ...args} as text.
			# Only accept that flattened form when schema-required fields exist.
			if not required_map.get(tool_name):
				continue
			raw_input = {
				key: value
				for key, value in obj.items()
				if key not in {"tool", "name", "tool_name"}
			}
			if not raw_input:
				continue

		tool_input, parse_ok = _coerce_tool_input(raw_input)
		if not parse_ok or tool_input is None:
			continue
		if _missing_required_fields(tool_name, tool_input, required_map):
			continue

		blocks.append(
			{
				"type": "tool_use",
				"id": f"toolu_json_intent_{int(time.time() * 1000)}_{len(blocks)}",
				"name": tool_name,
				"input": tool_input,
			}
		)
	return blocks


def _zai_tool_call_from_item(
	item: Any,
	available_tools: set[str],
) -> dict[str, Any] | None:
	"""Convert a single raw JSON call item into an Anthropic tool_use block."""
	if not isinstance(item, dict):
		return None
	function = item.get("function")
	if isinstance(function, dict):
		name = function.get("name")
		arguments = function.get("arguments")
	else:
		name = item.get("name") or item.get("tool")
		arguments = item.get("arguments")
	if arguments is None:
		arguments = item.get("input")
	if not isinstance(name, str) or name.strip() not in available_tools:
		return None
	tool_input, parse_ok = _coerce_tool_input(arguments)
	if not parse_ok or not isinstance(tool_input, dict):
		return None
	return {
		"type": "tool_use",
		"id": f"toolu_zai_{int(time.time() * 1000)}",
		"name": name.strip(),
		"input": tool_input,
	}


def _extract_zai_raw_tool_calls(
	text: str,
	data: dict[str, Any] | None = None,
) -> list[tuple[dict[str, Any], int, int]]:
	"""Parse Z.ai/GLM raw JSON or <invoke> XML tool payloads into tool_use blocks."""
	available_tools = _available_tool_names(data)
	if not available_tools:
		return []

	results: list[tuple[dict[str, Any], int, int]] = []

	# 1. JSON objects / arrays / wrapper objects with "tool_calls"
	if "{" in text or "[" in text:
		decoder = json.JSONDecoder()
		position = 0
		while position < len(text):
			obj_start = text.find("{", position)
			arr_start = text.find("[", position)
			if obj_start < 0 and arr_start < 0:
				break
			if arr_start >= 0 and (obj_start < 0 or arr_start < obj_start):
				try:
					value, end = decoder.raw_decode(text, arr_start)
				except json.JSONDecodeError:
					position = arr_start + 1
					continue
				if isinstance(value, list):
					for item in value:
						block = _zai_tool_call_from_item(item, available_tools)
						if block:
							results.append((block, arr_start, end))
				position = end
				continue
			try:
				value, end = decoder.raw_decode(text, obj_start)
			except json.JSONDecodeError:
				position = obj_start + 1
				continue
			if isinstance(value, dict):
				tool_calls = value.get("tool_calls")
				if isinstance(tool_calls, list):
					for item in tool_calls:
						block = _zai_tool_call_from_item(item, available_tools)
						if block:
							results.append((block, obj_start, end))
				else:
					block = _zai_tool_call_from_item(value, available_tools)
					if block:
						results.append((block, obj_start, end))
			position = end

	# 2. <invoke name="..."><parameter name="...">...</parameter></invoke>
	if "<invoke" in text.lower():
		for match in _ZAI_INVOKE_RE.finditer(text):
			tool_name = html.unescape(match.group(1)).strip()
			if tool_name not in available_tools:
				continue
			body = match.group(2)
			arguments: dict[str, Any] = {}
			for param_match in _ZAI_PARAMETER_RE.finditer(body):
				param_name = html.unescape(param_match.group(1)).strip()
				if param_name:
					arguments[param_name] = _maybe_json_parameter(param_match.group(2))
			if not arguments:
				field_name = _single_required_field_tool(data, tool_name)
				if field_name:
					raw_body = html.unescape(re.sub(r"(?is)<[^>]+>", "", body)).strip()
					if raw_body:
						arguments = {field_name: raw_body}
			if arguments:
				results.append(
					(
						{
							"type": "tool_use",
							"id": f"toolu_zai_xml_{int(time.time() * 1000)}_{len(results)}",
							"name": tool_name,
							"input": arguments,
						},
						match.start(),
						match.end(),
					)
				)

	return results


def _extract_native_tool_calls_with_spans(
	text: str,
	data: dict[str, Any] | None = None,
) -> list[tuple[dict[str, Any], int, int]]:
	results: list[tuple[dict[str, Any], int, int]] = []

	# 1. Kimi native
	if "<tool_call_begin>" in text:
		import json
		decoder = json.JSONDecoder()
		for match in _KIMI_TOOL_CALL_MARKER_RE.finditer(text):
			tool_name = match.group(1)
			position = match.end()
			while position < len(text) and text[position].isspace():
				position += 1
			try:
				arguments, end_idx = decoder.raw_decode(text, position)
			except Exception:
				continue
			if isinstance(arguments, dict):
				tool_use = {
					"type": "tool_use",
					"id": f"toolu_kimi_{int(time.time() * 1000)}_{len(results)}",
					"name": tool_name,
					"input": arguments,
				}
				results.append((tool_use, match.start(), end_idx))

	# 2. DeepSeek DSML
	if "DSML" in text:
		for match in _DEEPSEEK_DSML_INVOKE_RE.finditer(text):
			tool_name = html.unescape(match.group(1)).strip()
			if tool_name:
				body = match.group(2)
				arguments = {}
				for param_match in _DEEPSEEK_DSML_PARAM_RE.finditer(body):
					param_name = html.unescape(param_match.group(1)).strip()
					if param_name:
						arguments[param_name] = _maybe_json_parameter(param_match.group(2))
				tool_use = {
					"type": "tool_use",
					"id": f"toolu_dsml_{int(time.time() * 1000)}_{len(results)}",
					"name": tool_name,
					"input": arguments,
				}
				results.append((tool_use, match.start(), match.end()))

	# 3. Generic XML
	if "<tool_call" in text.lower():
		available_tools = _available_tool_names(data)
		if available_tools:
			for match in _GENERIC_TOOL_CALL_RE.finditer(text):
				tool_name = html.unescape(match.group(1)).strip()
				if tool_name in available_tools:
					body = match.group(2).strip()
					arguments = _json_object_from_text(html.unescape(body))
					if arguments is None:
						arguments = {}
						for param_match in _GENERIC_TOOL_PARAM_RE.finditer(body):
							param_name = html.unescape(param_match.group(1)).strip()
							if param_name:
								arguments[param_name] = _maybe_json_parameter(param_match.group(2))
					if not arguments:
						field_name = _single_required_field_tool(data, tool_name)
						if field_name:
							raw_body = html.unescape(re.sub(r"(?is)<[^>]+>", "", body)).strip()
							if raw_body:
								arguments = {field_name: raw_body}
					if isinstance(arguments, dict) and arguments:
						tool_use = {
							"type": "tool_use",
							"id": f"toolu_xml_{int(time.time() * 1000)}_{len(results)}",
							"name": tool_name,
							"input": arguments,
						}
						results.append((tool_use, match.start(), match.end()))

	# 4. File read tags
	if "<file-read" in text.lower() and _tool_is_available(data, "Read"):
		read_config = _read_tool_config(data or {})
		if read_config:
			tool_name, path_field = read_config
			for match in _FILE_READ_TAG_RE.finditer(text):
				attrs = {
					html.unescape(name).strip(): html.unescape(value).strip()
					for name, value in _XML_ATTR_RE.findall(match.group(1))
				}
				file_path = attrs.get("file_path") or attrs.get("path")
				if file_path:
					tool_use = {
						"type": "tool_use",
						"id": f"toolu_file_read_{int(time.time() * 1000)}_{len(results)}",
						"name": tool_name,
						"input": {path_field: file_path},
					}
					results.append((tool_use, match.start(), match.end()))

	# 5. Read file block
	if "<read_file" in text.lower() and _tool_is_available(data, "Read"):
		read_config = _read_tool_config(data or {})
		if read_config:
			tool_name, path_field = read_config
			for match in _READ_FILE_BLOCK_RE.finditer(text):
				body = match.group(1)
				path_match = _READ_FILE_PATH_RE.search(body)
				if path_match:
					file_path = html.unescape(re.sub(r"(?is)<[^>]+>", "", path_match.group(1))).strip()
					if file_path:
						tool_use = {
							"type": "tool_use",
							"id": f"toolu_read_file_{int(time.time() * 1000)}_{len(results)}",
							"name": tool_name,
							"input": {path_field: file_path},
						}
						results.append((tool_use, match.start(), match.end()))

	# 6. Function style
	available_tools = _available_tool_names(data)
	if available_tools and "({" in text:
		import json
		decoder = json.JSONDecoder()
		for tool_name in sorted(available_tools, key=len, reverse=True):
			marker = f"{tool_name}("
			position = 0
			while position < len(text):
				start = text.find(marker, position)
				if start < 0:
					break
				json_start = start + len(marker)
				try:
					arguments, json_end = decoder.raw_decode(text, json_start)
				except json.JSONDecodeError:
					position = start + len(marker)
					continue
				trailer = text[json_end:].lstrip()
				if trailer.startswith(")") and isinstance(arguments, dict):
					matched_end_idx = json_end + (len(text[json_end:]) - len(trailer)) + 1
					tool_use = {
						"type": "tool_use",
						"id": f"toolu_func_{int(time.time() * 1000)}_{len(results)}",
						"name": tool_name,
						"input": arguments,
					}
					results.append((tool_use, start, matched_end_idx))
					position = matched_end_idx
				else:
					position = json_end

	# 7. Bash XML
	if "<bash" in text.lower():
		tool_config = _single_required_command_tool(data or {})
		if tool_config:
			tool_name, command_field = tool_config
			for match in _BASH_XML_RE.finditer(text):
				body = match.group(1)
				command_match = _BASH_XML_COMMAND_RE.search(body)
				if command_match is not None:
					command = html.unescape(command_match.group(1)).strip()
				else:
					command = html.unescape(re.sub(r"(?is)<[^>]+>", "", body)).strip()
				if command:
					tool_use = {
						"type": "tool_use",
						"id": f"toolu_bash_xml_{int(time.time() * 1000)}_{len(results)}",
						"name": tool_name,
						"input": {command_field: command},
					}
					results.append((tool_use, match.start(), match.end()))

	# 8. Codex JSON command
	if "\"cmd\"" in text or "\"command\"" in text:
		tool_config = _single_required_command_tool(data or {})
		if tool_config:
			tool_name, command_field = tool_config
			import json
			decoder = json.JSONDecoder()
			position = 0
			while position < len(text):
				start = text.find("{", position)
				if start < 0:
					break
				try:
					value, end = decoder.raw_decode(text, start)
				except json.JSONDecodeError:
					position = start + 1
					continue
				if isinstance(value, dict):
					command = _command_from_codex_object(value)
					if command:
						tool_use = {
							"type": "tool_use",
							"id": f"toolu_codex_cmd_{int(time.time() * 1000)}_{len(results)}",
							"name": tool_name,
							"input": {command_field: command},
						}
						results.append((tool_use, start, end))
				position = end

	# 9. JSON tool intent
	if '"tool"' in text:
		available_tools = _available_tool_names(data)
		if available_tools:
			required_map = _required_fields_by_tool(data or {})
			import json
			decoder = json.JSONDecoder()
			position = 0
			while position < len(text):
				start = text.find("{", position)
				if start < 0:
					break
				try:
					value, end = decoder.raw_decode(text, start)
				except json.JSONDecodeError:
					position = start + 1
					continue
				if isinstance(value, dict):
					tool_name = value.get("tool")
					if isinstance(tool_name, str):
						tool_name = tool_name.strip()
						tool_name = _canonical_text_tool_name(tool_name, available_tools)
						if tool_name is not None:
							if "arguments" in value:
								raw_input = value.get("arguments")
							else:
								raw_input = {
									key: value_item
									for key, value_item in value.items()
									if key not in {"tool", "name", "tool_name"}
								}
							tool_input, parse_ok = _coerce_tool_input(raw_input)
							if parse_ok and tool_input is not None and not _missing_required_fields(tool_name, tool_input, required_map):
								tool_use = {
									"type": "tool_use",
									"id": f"toolu_json_intent_{int(time.time() * 1000)}_{len(results)}",
									"name": tool_name,
									"input": tool_input,
								}
								results.append((tool_use, start, end))
				position = end

	# 9b. Z.ai/GLM raw JSON / <invoke> XML
	model_name = str(data.get("model") or "").lower() if isinstance(data, dict) else ""
	if "glm" in model_name or "zai" in model_name:
		for tool_use, start, end in _extract_zai_raw_tool_calls(text, data):
			results.append((tool_use, start, end))

	# 10. Fenced code blocks
	if "```" in text:
		tool_config = _single_required_command_tool(data or {})
		if tool_config:
			tool_name, command_field = tool_config
			found_explicit = False
			for match in _BASH_EXPLICIT_FENCE_RE.finditer(text):
				command = match.group(1).strip()
				if command and len(command) <= 8000:
					tool_use = {
						"type": "tool_use",
						"id": f"toolu_fenced_cmd_{int(time.time() * 1000)}_{len(results)}",
						"name": tool_name,
						"input": {command_field: command},
					}
					results.append((tool_use, match.start(), match.end()))
					found_explicit = True
			
			if not found_explicit:
				for match in _BASH_GENERIC_FENCE_RE.finditer(text):
					command = match.group(1).strip()
					if command and len(command) <= 8000:
						first_line = next((line.strip() for line in command.splitlines() if line.strip()), "")
						if first_line and _looks_like_codex_shell_command(first_line):
							tool_use = {
								"type": "tool_use",
								"id": f"toolu_fenced_cmd_{int(time.time() * 1000)}_{len(results)}",
								"name": tool_name,
								"input": {command_field: command},
							}
							results.append((tool_use, match.start(), match.end()))

	return results


def _extract_native_text_tool_calls(
	text: str,
	data: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
	return [block for block, _, _ in _extract_native_tool_calls_with_spans(text, data)]


def _convert_native_text_tool_calls(response: Any, data: dict[str, Any] | None = None) -> int:
	"""Convert model-native textual tool calls into Anthropic `tool_use` blocks."""
	if response is None:
		return 0

	try:
		content = response.get("content") if isinstance(response, dict) else getattr(response, "content", None)
	except Exception:
		content = None

	if not isinstance(content, list):
		return 0

	text_blocks = [block for block in content if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)]
	if not text_blocks:
		return 0

	joined_text = "".join(block["text"] for block in text_blocks)
	
	extracted = _extract_native_tool_calls_with_spans(joined_text, data)
	if not extracted:
		return 0

	# Filter overlapping matches
	extracted.sort(key=lambda x: (x[1], -x[2]))
	filtered_results = []
	last_end = -1
	for tool_use, start, end in extracted:
		if start >= last_end:
			filtered_results.append((tool_use, start, end))
			last_end = end

	# Preserve any non-text blocks (e.g., images) from original content
	non_text_blocks = [
		block for block in content
		if not isinstance(block, dict) or block.get("type") != "text"
	]
	# Collect only the tool_use blocks
	tool_blocks = [tool_use for tool_use, _, _ in filtered_results]
	for tool_block in tool_blocks:
		_normalize_read_tool_input(
			tool_block.get("name"), tool_block.get("input")
		)
	# Build new content list
	new_content = non_text_blocks + tool_blocks
	if isinstance(response, dict):
		response["content"] = new_content
	else:
		try:
			response.content = new_content
		except Exception:
			pass
	_set_response_stop_reason(response, "tool_use")
	return len(filtered_results)


def _single_required_command_tool(data: dict[str, Any]) -> tuple[str, str] | None:
	required_map = _required_fields_by_tool(data)
	command_tools: list[tuple[str, str]] = []
	for tool_name, required in required_map.items():
		if len(required) == 1 and required[0] in {"command", "cmd"}:
			command_tools.append((tool_name, required[0]))

	for tool_name, command_field in command_tools:
		if tool_name == "Bash":
			return tool_name, command_field

	bash_like = [
		(tool_name, command_field)
		for tool_name, command_field in command_tools
		if tool_name.lower().endswith("bash")
	]
	if len(bash_like) == 1:
		return bash_like[0]
	if len(command_tools) == 1:
		return command_tools[0]
	return None


def _looks_like_shell_command(text: str) -> bool:
	stripped = text.strip()
	if not stripped or "\n" in stripped or "\r" in stripped:
		return False
	if len(stripped) > 500:
		return False
	if any(marker in stripped for marker in ("<antThinking", "<tool_call", "```")):
		return False
	if re.search(r"\b(can|should|would|here|terminal|script)\b", stripped, re.I):
		return False
	first_token = stripped.split(None, 1)[0]
	if first_token.upper() in {"OK", "DONE", "YES", "NO", "TRUE", "FALSE"}:
		return False
	if " " not in stripped and not first_token.startswith(("./", "/")):
		return False
	common_commands = {
		"awk",
		"bash",
		"cat",
		"chmod",
		"cp",
		"curl",
		"echo",
		"env",
		"find",
		"git",
		"grep",
		"head",
		"jq",
		"ls",
		"make",
		"mkdir",
		"mv",
		"node",
		"npm",
		"printf",
		"python",
		"python3",
		"rg",
		"sed",
		"sh",
		"tail",
		"test",
		"touch",
		"true",
		"wc",
	}
	if not first_token.startswith(("./", "/")) and first_token not in common_commands:
		return False
	return True


def _extract_bash_command_instruction(text: str) -> str | None:
	match = re.search(r"(?im)\bBash(?:\s+tool)?\s+with command:\s*(.+)$", text)
	if not match:
		return None
	command = _strip_wrapping_command_quotes(match.group(1))
	if not command or len(command) > 500:
		return None
	return command


def _extract_fenced_shell_command(text: str) -> str | None:
	match = _BASH_FENCE_RE.match(text)
	if not match:
		return None
	command = match.group(1).strip()
	if "\n" in command or "\r" in command:
		return None
	if not _looks_like_shell_command(command):
		return None
	return command


def _extract_fenced_code_block_tool_calls(
	text: str,
	data: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
	"""Extract fenced markdown command blocks from conversational text responses."""
	if "```" not in text:
		return []

	tool_config = _single_required_command_tool(data or {})
	if not tool_config:
		return []
	tool_name, command_field = tool_config

	blocks: list[dict[str, Any]] = []

	# 1. First scan for explicit bash/sh/shell code block fences
	for match in _BASH_EXPLICIT_FENCE_RE.finditer(text):
		command = match.group(1).strip()
		if not command or len(command) > 8000:
			continue
		blocks.append(
			{
				"type": "tool_use",
				"id": f"toolu_fenced_cmd_{int(time.time() * 1000)}_{len(blocks)}",
				"name": tool_name,
				"input": {command_field: command},
			}
		)

	# 2. Fall back to generic fences, but only if they contain shell-looking commands
	if not blocks:
		for match in _BASH_GENERIC_FENCE_RE.finditer(text):
			command = match.group(1).strip()
			if not command or len(command) > 8000:
				continue
			first_line = next((line.strip() for line in command.splitlines() if line.strip()), "")
			if first_line and _looks_like_codex_shell_command(first_line):
				blocks.append(
					{
						"type": "tool_use",
						"id": f"toolu_fenced_cmd_{int(time.time() * 1000)}_{len(blocks)}",
						"name": tool_name,
						"input": {command_field: command},
					}
				)

	return blocks


def _strip_wrapping_command_quotes(value: str) -> str:
	stripped = value.strip().strip("`").strip()
	if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in {"'", '"'}:
		return stripped[1:-1].strip()
	return stripped


def _message_text_content(message: dict[str, Any]) -> str:
	content = message.get("content")
	if isinstance(content, str):
		return content
	if not isinstance(content, list):
		return ""

	parts: list[str] = []
	for block in content:
		if not isinstance(block, dict):
			continue
		block_type = block.get("type")
		if block_type not in {"text", "input_text"}:
			continue
		text = block.get("text")
		if isinstance(text, str) and text:
			parts.append(text)
	return "\n".join(parts)


def _response_text_content(content: Any) -> str:
	if isinstance(content, str):
		return content
	if not isinstance(content, list):
		return ""

	parts: list[str] = []
	for block in content:
		if not isinstance(block, dict):
			continue
		block_type = block.get("type")
		if block_type in {"text", "input_text"}:
			text = block.get("text")
		elif block_type in {"thinking", "reasoning"}:
			text = block.get("thinking") or block.get("text")
		else:
			continue
		if isinstance(text, str) and text:
			parts.append(text)
	return "\n".join(parts)


def _count_assistant_tool_uses(messages: Any, tool_name: str) -> int:
	if not isinstance(messages, list):
		return 0

	count = 0
	for message in messages:
		if not isinstance(message, dict) or message.get("role") != "assistant":
			continue

		content = message.get("content")
		if isinstance(content, list):
			for block in content:
				if (
					isinstance(block, dict)
					and block.get("type") == "tool_use"
					and block.get("name") == tool_name
				):
					count += 1

		tool_calls = message.get("tool_calls")
		if isinstance(tool_calls, list):
			for tool_call in tool_calls:
				if not isinstance(tool_call, dict):
					continue
				function = tool_call.get("function")
				if not isinstance(function, dict):
					continue
				if function.get("name") == tool_name:
					count += 1

	return count


def _count_assistant_tool_uses_after(
	messages: Any,
	tool_name: str,
	start_index: int,
) -> int:
	if not isinstance(messages, list):
		return 0
	return _count_assistant_tool_uses(messages[start_index + 1 :], tool_name)


def _count_assistant_tool_uses_after_for_names(
	messages: Any,
	tool_names: list[str],
	start_index: int,
) -> int:
	if not isinstance(messages, list):
		return 0
	names = set(tool_names)
	if not names:
		return 0

	count = 0
	for message in messages[start_index + 1 :]:
		if not isinstance(message, dict) or message.get("role") != "assistant":
			continue

		content = message.get("content")
		if isinstance(content, list):
			for block in content:
				if (
					isinstance(block, dict)
					and block.get("type") == "tool_use"
					and block.get("name") in names
				):
					count += 1

		tool_calls = message.get("tool_calls")
		if isinstance(tool_calls, list):
			for tool_call in tool_calls:
				if not isinstance(tool_call, dict):
					continue
				function = tool_call.get("function")
				if not isinstance(function, dict):
					continue
				if function.get("name") in names:
					count += 1

	return count


def _tool_input_plan_value(tool_name: str, input_data: Any) -> str | None:
	if not isinstance(input_data, dict):
		return None
	if tool_name == "Bash":
		command = input_data.get("command")
		return command if isinstance(command, str) else None
	if tool_name == "Read":
		path = input_data.get("file_path") or input_data.get("path")
		return path if isinstance(path, str) else None
	return None


def _assistant_planned_tool_values(message: Any) -> list[tuple[str, str]]:
	if not isinstance(message, dict) or message.get("role") != "assistant":
		return []

	values: list[tuple[str, str]] = []
	content = message.get("content")
	if isinstance(content, list):
		for block in content:
			if not isinstance(block, dict) or block.get("type") != "tool_use":
				continue
			tool_name = block.get("name")
			if not isinstance(tool_name, str):
				continue
			value = _tool_input_plan_value(tool_name, block.get("input"))
			if value is not None:
				values.append((tool_name, value))

	tool_calls = message.get("tool_calls")
	if isinstance(tool_calls, list):
		for tool_call in tool_calls:
			if not isinstance(tool_call, dict):
				continue
			function = tool_call.get("function")
			if not isinstance(function, dict):
				continue
			tool_name = function.get("name")
			if not isinstance(tool_name, str):
				continue
			arguments = function.get("arguments")
			if isinstance(arguments, str):
				try:
					input_data = json.loads(arguments)
				except Exception:
					input_data = None
			else:
				input_data = arguments
			value = _tool_input_plan_value(tool_name, input_data)
			if value is not None:
				values.append((tool_name, value))

	return values


def _count_completed_planned_tool_steps_after(
	messages: Any,
	steps: list[tuple[str, str]],
	start_index: int,
) -> int:
	if not isinstance(messages, list) or not steps:
		return 0

	completed = 0
	for message in messages[start_index + 1 :]:
		if completed >= len(steps):
			break
		for actual_tool_name, actual_value in _assistant_planned_tool_values(message):
			if completed >= len(steps):
				break
			expected_tool_name, expected_value = steps[completed]
			if actual_tool_name == expected_tool_name and actual_value == expected_value:
				completed += 1

	return completed


def _latest_text_user_message_index(messages: Any) -> int | None:
	if not isinstance(messages, list):
		return None
	for index in range(len(messages) - 1, -1, -1):
		message = messages[index]
		if not isinstance(message, dict) or message.get("role") != "user":
			continue
		if _message_text_content(message).strip():
			return index
	return None


_READ_PATH_TOKEN_RE = re.compile(
	r"""(?x)
	(?:
		/[^,\s;)]+
		|~\/[^,\s;)]+
		|\.\.?\/[^,\s;)]+
		|[A-Za-z0-9_.-]+(?:/[^\s,;)]+)+
		|[A-Za-z0-9_.-]+\.(?:md|txt|json|ya?ml|toml|py|js|ts|tsx|jsx|html|css|sh)
	)
	"""
)


def _slash_command_phase_plan_text(text: str) -> str:
	"""Return only the active phase section from a slash-command skill body."""
	phase_match = re.search(
		r"(?im)^\s*Current invocation arguments:\s*(phase[\w.-]*)\b",
		text,
	)
	if not phase_match:
		return text
	phase = re.escape(phase_match.group(1))
	section = re.search(
		rf"(?ims)^\s*\d+\.\s*For\s+{phase}\b.*?(?=^\s*\d+\.\s*For\s+phase[\w.-]*\b|\Z)",
		text,
	)
	if not section:
		return text
	return section.group(0)


_PLANNED_BASH_PATTERN = re.compile(
	r"(?is)\b(?:Call the\s+)?Bash(?:\s+tool)?\s+with\s+command:\s*"
	r"(.+?)"
	r"(?=("
	r"\n\s*[-*]\s+(?:After|Call|Do not|Return|Then|Once|Append|Use)\b"
	r"|\.\s+(?=(?:After|Call|Do not|Return|Then|Once|Append|Use)\b)"
	r"|\.\s*(?=(?:Call\s+the\s+)?Bash(?:\s+tool)?\s+with\s+command:)"
	r"|\n\s*\d+\.\s+For\s+phase[\w.-]*\b"
	r"|\Z"
	r"))"
)


def _extract_planned_tool_steps(text: str) -> list[tuple[str, str]]:
	text = _slash_command_phase_plan_text(text)
	steps: list[tuple[int, str, str]] = []

	offset = 0
	for line in text.splitlines(keepends=True):
		instruction = re.search(
			r"(?i)\buse\s+(?:the\s+)?Read(?:\s+tool)?\s+(?:on|from|at)\s+(.+)$",
			line.rstrip("\n"),
		)
		if instruction:
			for match in _READ_PATH_TOKEN_RE.finditer(instruction.group(1)):
				path = match.group(0).strip().strip("`\"'").rstrip(".")
				if path:
					steps.append((offset + instruction.start(1) + match.start(), "Read", path))
		offset += len(line)

	for match in _PLANNED_BASH_PATTERN.finditer(text):
		command = _strip_wrapping_command_quotes(match.group(1))
		command = _strip_wrapping_command_quotes(command.rstrip("."))
		if not command or len(command) > 500:
			continue
		if _looks_like_shell_command(command):
			steps.append((match.start(), "Bash", command))

	steps.sort(key=lambda item: item[0])
	ordered: list[tuple[str, str]] = []
	for _, tool_name, value in steps:
		ordered.append((tool_name, value))
	return ordered


def _extract_planned_read_paths(text: str) -> list[str]:
	return [value for tool_name, value in _extract_planned_tool_steps(text) if tool_name == "Read"]


def _read_tool_config(data: dict[str, Any]) -> tuple[str, str] | None:
	if not _tool_is_available(data, "Read"):
		return None
	required = _required_fields_by_tool(data).get("Read") or []
	if "file_path" in required:
		return "Read", "file_path"
	if "path" in required:
		return "Read", "path"
	return "Read", "file_path"


def _latest_user_message_with_plans(
	messages: list[Any],
	extract_plans,
) -> tuple[int, list[str]] | None:
	for index in range(len(messages) - 1, -1, -1):
		message = messages[index]
		if not isinstance(message, dict) or message.get("role") != "user":
			continue
		text = _message_text_content(message).strip()
		if not text:
			continue
		plans = extract_plans(text)
		if plans:
			return index, plans
	return None


def _next_planned_read_path(data: dict[str, Any], tool_name: str) -> str | None:
	return _next_planned_tool_value(data, tool_name)


def _set_response_stop_reason(response: Any, stop_reason: str) -> None:
	if isinstance(response, dict):
		response["stop_reason"] = stop_reason
		return
	try:
		setattr(response, "stop_reason", stop_reason)
	except Exception:
		pass


def _duration_ms(start_time: Any, end_time: Any) -> float | None:
	try:
		delta = end_time - start_time
		if hasattr(delta, "total_seconds"):
			return round(delta.total_seconds() * 1000, 2)
		return round(float(delta) * 1000, 2)
	except Exception:
		return None


def _failure_message(response_obj: Any) -> str:
	for attr in ("message", "detail", "body"):
		try:
			value = getattr(response_obj, attr)
		except Exception:
			continue
		if value:
			return str(value)[:500].replace("\n", " ")
	if isinstance(response_obj, dict):
		for key in ("message", "error", "detail"):
			value = response_obj.get(key)
			if value:
				return str(value)[:500].replace("\n", " ")
	text = str(response_obj)
	return text[:500].replace("\n", " ") if text else ""


def _extract_planned_shell_commands(text: str) -> list[str]:
	return [value for tool_name, value in _extract_planned_tool_steps(text) if tool_name == "Bash"]


def _next_planned_tool_value(data: dict[str, Any], tool_name: str) -> str | None:
	messages = data.get("messages")
	if not isinstance(messages, list):
		return None

	match = _latest_user_message_with_plans(messages, _extract_planned_tool_steps)
	if match is None:
		return None
	index, steps = match
	used_tools = _count_completed_planned_tool_steps_after(messages, steps, index)
	if used_tools < len(steps):
		next_tool_name, value = steps[used_tools]
		if next_tool_name == tool_name:
			return value
	return None


def _next_planned_shell_command(data: dict[str, Any], tool_name: str) -> str | None:
	return _next_planned_tool_value(data, tool_name)


def _looks_like_generic_tool_ack(text: str) -> bool:
	stripped = text.strip()
	if not stripped:
		return True
	if len(stripped) > 240:
		return False

	lowered = stripped.lower()
	markers = (
		"i'll execute",
		"i will execute",
		"i'll run",
		"i will run",
		"i'll call",
		"i will call",
		"i'll perform",
		"i will perform",
		"i'll handle",
		"i will handle",
		"no prose response",
		"no prose until final output",
		"ready to proceed",
		"understood",
		"got it",
	)
	if any(marker in lowered for marker in markers):
		return True
	return bool(re.match(r"(?i)^(?:i('| a)m|i will|i'll|sure|okay|ok|done|ready)\b", stripped))


def _planned_tool_use_command(data: dict[str, Any], tool_name: str) -> str | None:
	return _next_planned_shell_command(data, tool_name)


def _convert_single_tool_text_command(response: Any, data: dict[str, Any]) -> int:
	"""Convert a bare command string into a single Anthropic `tool_use` block."""
	tool_config = _single_required_command_tool(data)
	if not tool_config or response is None:
		return 0
	tool_name, command_field = tool_config

	try:
		content = response.get("content") if isinstance(response, dict) else getattr(response, "content", None)
	except Exception:
		content = None
	if not isinstance(content, list):
		return 0
	if any(isinstance(block, dict) and block.get("type") == "tool_use" for block in content):
		return 0
	text = _response_text_content(content).strip()
	command = text.strip() if _looks_like_shell_command(text) else None
	if command is None:
		command = _extract_bash_command_instruction(text)
	if command is None:
		command = _extract_fenced_shell_command(text)
	if command is None:
		command = _planned_tool_use_command(data, tool_name)
	if not command:
		return 0
	new_content = [
		{
			"type": "tool_use",
			"id": f"toolu_cmd_{int(time.time() * 1000)}",
			"name": tool_name,
			"input": {command_field: command},
		}
	]
	if isinstance(response, dict):
		response["content"] = new_content
	else:
		try:
			response.content = new_content
		except Exception:
			return 0
	_set_response_stop_reason(response, "tool_use")
	return 1


def _convert_planned_read_tool(response: Any, data: dict[str, Any]) -> int:
	tool_config = _read_tool_config(data)
	if not tool_config or response is None:
		return 0
	tool_name, path_field = tool_config

	try:
		content = response.get("content") if isinstance(response, dict) else getattr(response, "content", None)
	except Exception:
		content = None
	if not isinstance(content, list):
		return 0
	if any(isinstance(block, dict) and block.get("type") == "tool_use" for block in content):
		return 0

	path = _next_planned_read_path(data, tool_name)
	if not path:
		return 0
	new_content = [
		{
			"type": "tool_use",
			"id": f"toolu_read_{int(time.time() * 1000)}",
			"name": tool_name,
			"input": {path_field: path},
		}
	]
	if isinstance(response, dict):
		response["content"] = new_content
	else:
		try:
			response.content = new_content
		except Exception:
			return 0
	_set_response_stop_reason(response, "tool_use")
	return 1


_PLANNED_AGENT_PATTERN = re.compile(
	r"(?is)\bUse\s+the\s+Agent\s+tool\s+exactly\s+once\s+with\s+subagent_type\s+"
	r"(?P<subagent>[A-Za-z0-9_.:-]+)\.\s+Delegate\s+this\s+prompt:\s+"
	r"(?P<prompt>.*?)(?:\s+Do\s+not\s+answer\s+directly\b|\s+After\s+the\s+Agent\s+tool\s+result\b|\Z)"
)
_PLANNED_AGENT_FINAL_PATTERN = re.compile(
	r"(?is)\bAfter\s+the\s+Agent\s+tool\s+result\s+is\s+returned,\s+"
	r"reply\s+exactly\s+(?P<final>.+?)\s+and\s+no\s+other\s+text\b"
)


def _agent_tool_name(data: dict[str, Any]) -> str | None:
	for tool_name in ("Agent", "Task"):
		if _tool_is_available(data, tool_name):
			required = set(_required_fields_by_tool(data).get(tool_name) or [])
			if {"description", "prompt", "subagent_type"}.issubset(required):
				return tool_name
	return None


def _planned_agent_tool_input(data: dict[str, Any]) -> tuple[str, dict[str, str]] | None:
	tool_name = _agent_tool_name(data)
	if not tool_name:
		return None
	messages = data.get("messages")
	if not isinstance(messages, list):
		return None
	user_index = _latest_text_user_message_index(messages)
	if user_index is None:
		return None
	if _count_assistant_tool_uses_after_for_names(messages, ["Agent", "Task"], user_index) > 0:
		return None
	text = _message_text_content(messages[user_index]).strip()
	if not text:
		return None
	match = _PLANNED_AGENT_PATTERN.search(text)
	if not match:
		return None
	subagent_type = match.group("subagent").strip()
	prompt = " ".join(match.group("prompt").strip().split())
	if not subagent_type or not prompt:
		return None
	description = f"Delegate deterministic smoke task to {subagent_type}"
	return tool_name, {
		"description": description,
		"prompt": prompt,
		"subagent_type": subagent_type,
	}


def _convert_planned_agent_tool(response: Any, data: dict[str, Any]) -> int:
	planned = _planned_agent_tool_input(data)
	if not planned or response is None:
		return 0
	tool_name, tool_input = planned

	try:
		content = response.get("content") if isinstance(response, dict) else getattr(response, "content", None)
	except Exception:
		content = None
	if not isinstance(content, list):
		return 0
	if any(isinstance(block, dict) and block.get("type") == "tool_use" for block in content):
		return 0

	text = _response_text_content(content).strip()
	if text and not _looks_like_generic_tool_ack(text):
		return 0

	new_content = [
		{
			"type": "tool_use",
			"id": f"toolu_agent_{int(time.time() * 1000)}",
			"name": tool_name,
			"input": tool_input,
		}
	]
	if isinstance(response, dict):
		response["content"] = new_content
	else:
		try:
			response.content = new_content
		except Exception:
			return 0
	_set_response_stop_reason(response, "tool_use")
	return 1


def _has_tool_result_after(messages: Any, start_index: int) -> bool:
	if not isinstance(messages, list):
		return False
	for message in messages[start_index + 1 :]:
		if not isinstance(message, dict):
			continue
		if message.get("role") == "tool":
			return True
		content = message.get("content")
		if not isinstance(content, list):
			continue
		if any(isinstance(block, dict) and block.get("type") == "tool_result" for block in content):
			return True
	return False


def _planned_agent_final_text(data: dict[str, Any]) -> str | None:
	messages = data.get("messages")
	if not isinstance(messages, list):
		return None
	user_index = _latest_text_user_message_index(messages)
	if user_index is None:
		return None
	text = _message_text_content(messages[user_index]).strip()
	if not text:
		return None
	match = _PLANNED_AGENT_FINAL_PATTERN.search(text)
	if not match:
		return None
	final = " ".join(match.group("final").strip().split())
	if not final:
		return None
	if _count_assistant_tool_uses_after_for_names(messages, ["Agent", "Task"], user_index) < 1:
		return None
	if not _has_tool_result_after(messages, user_index):
		return None
	return final


def _set_response_text_content(response: Any, text: str) -> bool:
	new_content = [{"type": "text", "text": text}]
	if isinstance(response, dict):
		response["content"] = new_content
	else:
		try:
			response.content = new_content
		except Exception:
			return False
	_set_response_stop_reason(response, "end_turn")
	return True


def _repair_planned_agent_final_text(response: Any, data: dict[str, Any]) -> int:
	final = _planned_agent_final_text(data)
	if not final or response is None:
		return 0
	try:
		content = response.get("content") if isinstance(response, dict) else getattr(response, "content", None)
	except Exception:
		content = None
	if not isinstance(content, list):
		return 0
	if any(isinstance(block, dict) and block.get("type") == "tool_use" for block in content):
		return 0
	text = _response_text_content(content).strip()
	if text and not _looks_like_generic_tool_ack(text):
		return 0
	return 1 if _set_response_text_content(response, final) else 0


def _stream_item_text(item: Any) -> str | None:
	if isinstance(item, bytes):
		return item.decode("utf-8", "replace")
	if isinstance(item, str):
		return item
	if isinstance(item, dict):
		try:
			return json.dumps(item)
		except Exception:
			return None
	try:
		model_dump = getattr(item, "model_dump", None)
		if callable(model_dump):
			return json.dumps(model_dump(mode="json", exclude_none=True))
	except Exception:
		return None
	return None


def _stream_item_events(item: Any) -> list[tuple[str | None, dict[str, Any]]]:
	if isinstance(item, dict):
		event_type = item.get("type") if isinstance(item.get("type"), str) else None
		return [(event_type, item)]

	text = _stream_item_text(item)
	if not text:
		return []

	events: list[tuple[str | None, dict[str, Any]]] = []
	for frame in text.split("\n\n"):
		event_type: str | None = None
		data_lines: list[str] = []
		for line in frame.splitlines():
			line = line.strip()
			if line.startswith("event:"):
				event_type = line.split("event:", 1)[1].strip() or None
			elif line.startswith("data:"):
				data_lines.append(line.split("data:", 1)[1].strip())
		if data_lines:
			for data_line in data_lines:
				if not data_line or data_line == "[DONE]":
					continue
				try:
					payload = json.loads(data_line)
				except Exception:
					continue
				if isinstance(payload, dict):
					events.append((event_type or payload.get("type"), payload))
			continue

		try:
			payload = json.loads(frame)
		except Exception:
			continue
		if isinstance(payload, dict):
			events.append((payload.get("type"), payload))
	return events


def _sse_event(event: str, payload: dict[str, Any]) -> str:
	return f"event: {event}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"


class _AnthropicSSEStreamSanitizer:
	def __init__(self) -> None:
		self.block_types: dict[int, str] = {}
		self.buffer = ""
		self.dropped_thinking_deltas = 0

	def _sanitize_payload(
		self,
		event_type: str | None,
		payload: dict[str, Any],
	) -> bool:
		payload_type = payload.get("type") or event_type
		if payload_type == "content_block_start":
			index = payload.get("index")
			block = payload.get("content_block")
			if isinstance(index, int) and isinstance(block, dict):
				block_type = block.get("type")
				if isinstance(block_type, str):
					self.block_types[index] = block_type
			return True

		if payload_type == "content_block_stop":
			index = payload.get("index")
			if isinstance(index, int):
				self.block_types.pop(index, None)
			return True

		if payload_type != "content_block_delta":
			return True

		index = payload.get("index")
		delta = payload.get("delta")
		if not isinstance(index, int) or not isinstance(delta, dict):
			return True
		if delta.get("type") != "thinking_delta":
			return True
		if self.block_types.get(index) == "thinking":
			return True

		self.dropped_thinking_deltas += 1
		return False

	def _sanitize_frame(self, frame: str) -> str | None:
		event_type: str | None = None
		data_line_indices: list[int] = []
		lines = frame.splitlines()
		for index, line in enumerate(lines):
			stripped = line.strip()
			if stripped.startswith("event:"):
				event_type = stripped.split("event:", 1)[1].strip() or None
			elif stripped.startswith("data:"):
				data_line_indices.append(index)

		if len(data_line_indices) != 1:
			return frame + "\n\n"

		data_index = data_line_indices[0]
		data_text = lines[data_index].split("data:", 1)[1].strip()
		if not data_text or data_text == "[DONE]":
			return frame + "\n\n"

		try:
			payload = json.loads(data_text)
		except Exception:
			return frame + "\n\n"
		if not isinstance(payload, dict):
			return frame + "\n\n"

		if not self._sanitize_payload(event_type, payload):
			return None
		return frame + "\n\n"

	def feed(self, item: Any) -> list[Any]:
		if isinstance(item, dict):
			event_type = item.get("type") if isinstance(item.get("type"), str) else None
			if self._sanitize_payload(event_type, item):
				return [item]
			return []

		text = _stream_item_text(item)
		if text is None:
			return [item]

		self.buffer += text
		output: list[Any] = []
		while "\n\n" in self.buffer:
			frame, self.buffer = self.buffer.split("\n\n", 1)
			sanitized = self._sanitize_frame(frame)
			if sanitized is not None:
				output.append(sanitized)
		return output

	def flush(self) -> list[Any]:
		if not self.buffer:
			return []
		frame = self.buffer
		self.buffer = ""
		sanitized = self._sanitize_frame(frame.rstrip("\n"))
		return [] if sanitized is None else [sanitized]


def _sanitize_anthropic_sse_stream_items(buffered: list[Any]) -> tuple[list[Any], int]:
	sanitizer = _AnthropicSSEStreamSanitizer()
	output: list[Any] = []
	for item in buffered:
		output.extend(sanitizer.feed(item))
	output.extend(sanitizer.flush())
	return output, sanitizer.dropped_thinking_deltas


def _anthropic_tool_blocks_stream(
	tool_blocks: list[dict[str, Any]],
	message_start: dict[str, Any] | None,
	message_delta_usage: dict[str, Any] | None,
) -> list[str]:
	if not tool_blocks:
		return []

	if not isinstance(message_start, dict):
		message_start = {
			"type": "message_start",
			"message": {
				"id": f"msg_stream_{int(time.time() * 1000)}",
				"type": "message",
				"role": "assistant",
				"content": [],
				"model": "unknown",
				"stop_reason": None,
				"stop_sequence": None,
				"usage": {"input_tokens": 0, "output_tokens": 0},
			},
		}
	else:
		message = message_start.get("message")
		if isinstance(message, dict):
			message["content"] = []
			message["stop_reason"] = None
			message["stop_sequence"] = None

	events = [_sse_event("message_start", message_start)]
	for index, block in enumerate(tool_blocks):
		tool_name = block.get("name")
		if not isinstance(tool_name, str) or not tool_name.strip():
			continue
		tool_input = block.get("input")
		if not isinstance(tool_input, dict):
			tool_input = {}
		else:
			tool_input = dict(tool_input)
		_normalize_read_tool_input(tool_name, tool_input)
		tool_id = block.get("id")
		if not isinstance(tool_id, str) or not tool_id.strip():
			tool_id = f"toolu_stream_{int(time.time() * 1000)}_{index}"
		events.extend(
			[
				_sse_event(
					"content_block_start",
					{
						"type": "content_block_start",
						"index": index,
						"content_block": {
							"type": "tool_use",
							"id": tool_id,
							"name": tool_name,
							"input": {},
						},
					},
				),
				_sse_event(
					"content_block_delta",
					{
						"type": "content_block_delta",
						"index": index,
						"delta": {
							"type": "input_json_delta",
							"partial_json": json.dumps(
								tool_input,
								separators=(",", ":"),
								ensure_ascii=False,
							),
						},
					},
				),
				_sse_event(
					"content_block_stop",
					{"type": "content_block_stop", "index": index},
				),
			]
		)
	events.extend(
		[
			_sse_event(
				"message_delta",
				{
					"type": "message_delta",
					"delta": {"stop_reason": "tool_use", "stop_sequence": None},
					"usage": message_delta_usage or {"output_tokens": 0},
				},
			),
			_sse_event("message_stop", {"type": "message_stop"}),
		]
	)
	return events


def _anthropic_tool_use_stream(
	tool_name: str,
	command_field: str,
	command: str,
	message_start: dict[str, Any] | None,
	message_delta_usage: dict[str, Any] | None,
) -> list[str]:
	return _anthropic_tool_blocks_stream(
		[
			{
				"type": "tool_use",
				"id": f"toolu_stream_{int(time.time() * 1000)}",
				"name": tool_name,
				"input": {command_field: command},
			}
		],
		message_start,
		message_delta_usage,
	)


def _anthropic_text_stream(
	text: str,
	message_start: dict[str, Any] | None,
	message_delta_usage: dict[str, Any] | None,
) -> list[str]:
	if not isinstance(message_start, dict):
		message_start = {
			"type": "message_start",
			"message": {
				"id": f"msg_stream_{int(time.time() * 1000)}",
				"type": "message",
				"role": "assistant",
				"content": [],
				"model": "unknown",
				"stop_reason": None,
				"stop_sequence": None,
				"usage": {"input_tokens": 0, "output_tokens": 0},
			},
		}
	else:
		message = message_start.get("message")
		if isinstance(message, dict):
			message["content"] = []
			message["stop_reason"] = None
			message["stop_sequence"] = None

	return [
		_sse_event("message_start", message_start),
		_sse_event(
			"content_block_start",
			{
				"type": "content_block_start",
				"index": 0,
				"content_block": {"type": "text", "text": ""},
			},
		),
		_sse_event(
			"content_block_delta",
			{
				"type": "content_block_delta",
				"index": 0,
				"delta": {"type": "text_delta", "text": text},
			},
		),
		_sse_event(
			"content_block_stop",
			{"type": "content_block_stop", "index": 0},
		),
		_sse_event(
			"message_delta",
			{
				"type": "message_delta",
				"delta": {"stop_reason": "end_turn", "stop_sequence": None},
				"usage": message_delta_usage or {"output_tokens": 0},
			},
		),
		_sse_event("message_stop", {"type": "message_stop"}),
	]


def _planned_command_replacement(data: dict[str, Any]) -> tuple[str, str, str] | None:
	tool_config = _single_required_command_tool(data)
	if not tool_config:
		return None
	tool_name, command_field = tool_config
	command = _planned_tool_use_command(data, tool_name)
	if not command:
		return None
	return tool_name, command_field, command


def _planned_tool_replacement(data: dict[str, Any]) -> tuple[str, str, str] | None:
	command_replacement = _planned_command_replacement(data)
	if command_replacement is not None:
		return command_replacement

	read_config = _read_tool_config(data)
	if not read_config:
		return None
	tool_name, path_field = read_config
	path = _next_planned_read_path(data, tool_name)
	if not path:
		return None
	return tool_name, path_field, path


def _rewrite_planned_tool_call(response: Any, data: dict[str, Any]) -> int:
	replacement = _planned_tool_replacement(data)
	if replacement is None or response is None:
		return 0
	tool_name, input_field, expected_value = replacement

	try:
		content = response.get("content") if isinstance(response, dict) else getattr(response, "content", None)
	except Exception:
		content = None
	if not isinstance(content, list):
		return 0

	tool_blocks = [
		block
		for block in content
		if isinstance(block, dict) and block.get("type") == "tool_use"
	]
	if len(tool_blocks) != 1:
		return 0

	block = tool_blocks[0]
	actual_tool_name = block.get("name")
	parsed, parse_ok = _coerce_tool_input(block.get("input"))
	actual_value = (
		_tool_input_plan_value(actual_tool_name, parsed)
		if isinstance(actual_tool_name, str) and parse_ok and isinstance(parsed, dict)
		else None
	)
	if actual_tool_name == tool_name and actual_value == expected_value:
		return 0

	block["name"] = tool_name
	block["input"] = {input_field: expected_value}
	_set_response_stop_reason(response, "tool_use")
	return 1


def _rewrite_planned_command_tool_call(response: Any, data: dict[str, Any]) -> int:
	return _rewrite_planned_tool_call(response, data)


def _streamed_planned_tool_rewrite_events(
	buffered: list[Any],
	data: dict[str, Any],
) -> list[str] | None:
	replacement = _planned_tool_replacement(data)
	if replacement is None:
		return None
	tool_name, input_field, expected_value = replacement

	message_start: dict[str, Any] | None = None
	message_delta_usage: dict[str, Any] | None = None
	tool_blocks: dict[int, dict[str, Any]] = {}

	for item in buffered:
		for event_type, payload in _stream_item_events(item):
			payload_type = payload.get("type") or event_type
			if payload_type == "message_start":
				message_start = payload
			elif payload_type == "message_delta":
				usage = payload.get("usage")
				if isinstance(usage, dict):
					message_delta_usage = usage
			elif payload_type == "content_block_start":
				index = payload.get("index")
				block = payload.get("content_block")
				if not isinstance(index, int) or not isinstance(block, dict):
					continue
				if block.get("type") != "tool_use":
					continue
				initial_input = block.get("input")
				tool_blocks[index] = {
					"id": block.get("id"),
					"name": block.get("name"),
					"input": dict(initial_input) if isinstance(initial_input, dict) else {},
					"parts": [],
				}
			elif payload_type == "content_block_delta":
				index = payload.get("index")
				delta = payload.get("delta")
				if not isinstance(index, int) or not isinstance(delta, dict):
					continue
				if delta.get("type") != "input_json_delta":
					continue
				block = tool_blocks.setdefault(
					index,
					{"id": None, "name": None, "input": {}, "parts": []},
				)
				partial_json = delta.get("partial_json")
				if isinstance(partial_json, str):
					block["parts"].append(partial_json)

	if len(tool_blocks) != 1:
		return None
	block = next(iter(tool_blocks.values()))

	input_data = dict(block.get("input") or {})
	partial_json = "".join(block.get("parts") or [])
	parse_ok = True
	if partial_json.strip():
		parsed, parse_ok = _coerce_tool_input(partial_json)
		if parse_ok and isinstance(parsed, dict):
			input_data.update(parsed)
	actual_tool_name = block.get("name")
	actual_value = (
		_tool_input_plan_value(actual_tool_name, input_data)
		if isinstance(actual_tool_name, str) and parse_ok
		else None
	)
	if actual_tool_name == tool_name and actual_value == expected_value:
		return None

	return _anthropic_tool_blocks_stream(
		[
			{
				"type": "tool_use",
				"id": f"toolu_stream_{int(time.time() * 1000)}",
				"name": tool_name,
				"input": {input_field: expected_value},
			}
		],
		message_start,
		message_delta_usage,
	)


def _streamed_planned_command_rewrite_events(
	buffered: list[Any],
	data: dict[str, Any],
) -> list[str] | None:
	return _streamed_planned_tool_rewrite_events(buffered, data)


def _streamed_command_tool_events(
	buffered: list[Any],
	data: dict[str, Any],
) -> list[str] | None:
	tool_config = _single_required_command_tool(data)
	if not tool_config:
		return None
	tool_name, command_field = tool_config

	text_parts: list[str] = []
	thinking_parts: list[str] = []
	message_start: dict[str, Any] | None = None
	message_delta_usage: dict[str, Any] | None = None
	tool_event_seen = False

	for item in buffered:
		for event_type, payload in _stream_item_events(item):
			payload_type = payload.get("type") or event_type
			if payload_type == "message_start":
				message_start = payload
			elif payload_type == "content_block_start":
				block = payload.get("content_block")
				if isinstance(block, dict) and block.get("type") == "tool_use":
					tool_event_seen = True
			elif payload_type == "content_block_delta":
				delta = payload.get("delta")
				if not isinstance(delta, dict):
					continue
				if delta.get("type") == "input_json_delta":
					tool_event_seen = True
				elif delta.get("type") == "text_delta":
					text = delta.get("text")
					if isinstance(text, str):
						text_parts.append(text)
				elif delta.get("type") == "thinking_delta":
					thinking = delta.get("thinking")
					if isinstance(thinking, str):
						thinking_parts.append(thinking)
			elif payload_type == "message_delta":
				usage = payload.get("usage")
				if isinstance(usage, dict):
					message_delta_usage = usage

	if tool_event_seen:
		return None

	text = "".join(text_parts).strip()
	if not text:
		text = "".join(thinking_parts).strip()
	command = text if _looks_like_shell_command(text) else None
	if command is None:
		command = _extract_bash_command_instruction(text)
	if command is None:
		command = _extract_fenced_shell_command(text)
	if command is None:
		command = _planned_tool_use_command(data, tool_name)
	if not command:
		return None
	return _anthropic_tool_use_stream(
		tool_name,
		command_field,
		command,
		message_start,
		message_delta_usage,
	)


def _streamed_native_tool_events(
	buffered: list[Any],
	data: dict[str, Any],
) -> list[str] | None:
	text_parts: list[str] = []
	thinking_parts: list[str] = []
	message_start: dict[str, Any] | None = None
	message_delta_usage: dict[str, Any] | None = None
	tool_event_seen = False

	for item in buffered:
		for event_type, payload in _stream_item_events(item):
			payload_type = payload.get("type") or event_type
			if payload_type == "message_start":
				message_start = payload
			elif payload_type == "content_block_start":
				block = payload.get("content_block")
				if isinstance(block, dict) and block.get("type") == "tool_use":
					tool_event_seen = True
			elif payload_type == "content_block_delta":
				delta = payload.get("delta")
				if not isinstance(delta, dict):
					continue
				if delta.get("type") == "input_json_delta":
					tool_event_seen = True
				elif delta.get("type") == "text_delta":
					text = delta.get("text")
					if isinstance(text, str):
						text_parts.append(text)
				elif delta.get("type") in {"thinking_delta", "reasoning_delta"}:
					for key in ("thinking", "reasoning", "text", "content"):
						text = delta.get(key)
						if isinstance(text, str):
							thinking_parts.append(text)
							break
			elif payload_type == "message_delta":
				usage = payload.get("usage")
				if isinstance(usage, dict):
					message_delta_usage = usage

	if tool_event_seen:
		return None

	tool_blocks: list[dict[str, Any]] = []
	for source_text in ("".join(text_parts), "".join(thinking_parts)):
		if not source_text:
			continue
		tool_blocks = _extract_native_text_tool_calls(source_text, data)
		if tool_blocks:
			break
	if not tool_blocks:
		read_config = _read_tool_config(data)
		if read_config:
			tool_name, path_field = read_config
			path = _next_planned_read_path(data, tool_name)
			if path:
				tool_blocks = [
					{
						"type": "tool_use",
							"id": f"toolu_stream_read_{int(time.time() * 1000)}",
							"name": tool_name,
							"input": {path_field: path},
						}
					]
	if not tool_blocks:
		planned_agent = _planned_agent_tool_input(data)
		if planned_agent:
			tool_name, tool_input = planned_agent
			tool_blocks = [
				{
					"type": "tool_use",
					"id": f"toolu_stream_agent_{int(time.time() * 1000)}",
					"name": tool_name,
					"input": tool_input,
				}
			]
	if not tool_blocks:
		return None
	return _anthropic_tool_blocks_stream(
		tool_blocks,
		message_start,
		message_delta_usage,
	)


def _streamed_planned_agent_final_events(
	buffered: list[Any],
	data: dict[str, Any],
) -> list[str] | None:
	final = _planned_agent_final_text(data)
	if not final:
		return None

	text_parts: list[str] = []
	thinking_parts: list[str] = []
	message_start: dict[str, Any] | None = None
	message_delta_usage: dict[str, Any] | None = None
	tool_event_seen = False

	for item in buffered:
		for event_type, payload in _stream_item_events(item):
			payload_type = payload.get("type") or event_type
			if payload_type == "message_start":
				message_start = payload
			elif payload_type == "content_block_start":
				block = payload.get("content_block")
				if isinstance(block, dict) and block.get("type") == "tool_use":
					tool_event_seen = True
			elif payload_type == "content_block_delta":
				delta = payload.get("delta")
				if not isinstance(delta, dict):
					continue
				if delta.get("type") == "input_json_delta":
					tool_event_seen = True
				elif delta.get("type") == "text_delta":
					text = delta.get("text")
					if isinstance(text, str):
						text_parts.append(text)
				elif delta.get("type") in {"thinking_delta", "reasoning_delta"}:
					for key in ("thinking", "reasoning", "text", "content"):
						text = delta.get(key)
						if isinstance(text, str):
							thinking_parts.append(text)
							break
			elif payload_type == "message_delta":
				usage = payload.get("usage")
				if isinstance(usage, dict):
					message_delta_usage = usage

	if tool_event_seen:
		return None
	text = "".join(text_parts).strip()
	if not text:
		text = "".join(thinking_parts).strip()
	if text and not _looks_like_generic_tool_ack(text):
		return None
	return _anthropic_text_stream(final, message_start, message_delta_usage)


def _should_buffer_command_tool_stream(data: dict[str, Any]) -> bool:
	if not _single_required_command_tool(data):
		return False
	return True


def _should_buffer_tool_stream(data: dict[str, Any]) -> bool:
	if not _is_anthropic_messages_stream(data):
		return False
	if _planned_agent_final_text(data):
		return True
	if _should_buffer_command_tool_stream(data):
		return True
	tools = data.get("tools")
	return isinstance(tools, list) and bool(tools)


def _is_anthropic_messages_stream(data: dict[str, Any]) -> bool:
	"""Return true for Anthropic `/v1/messages` traffic.

	The stream-buffer repair emits Anthropic SSE frames and enforces a short
	first-item timeout. Applying it to OpenAI `/v1/chat/completions` streams can
	turn a slow first token into a proxy 500, so chat streams must pass through.
	"""
	proxy_request = data.get("proxy_server_request")
	if isinstance(proxy_request, dict):
		url = proxy_request.get("url")
		if isinstance(url, str) and url.strip():
			path = urlsplit(url).path.rstrip("/")
			return path.endswith("/v1/messages") or path.endswith("/anthropic/v1/messages")
		return False

	metadata = data.get("metadata")
	if isinstance(metadata, dict) and metadata.get("ccproxy_is_passthrough") is True:
		return True

	# Unit tests call the hook directly with Anthropic-shaped chunks and no
	# FastAPI request metadata. Keep that synthetic path on the guarded branch.
	return "proxy_server_request" not in data


def _sanitize_response_text_artifacts(response: Any) -> int:
	"""Scrub literal think tags from Anthropic and OpenAI response shapes."""
	if response is None:
		return 0

	patched = 0
	try:
		content = (
			response.get("content")
			if isinstance(response, dict)
			else getattr(response, "content", None)
		)
	except Exception:
		content = None

	if isinstance(content, list):
		new_content = []
		for block in content:
			if not isinstance(block, dict) or block.get("type") != "text":
				new_content.append(block)
				continue
			text = block.get("text")
			if not isinstance(text, str):
				new_content.append(block)
				continue
			cleaned, changed = _strip_think_tag_artifacts(text)
			if changed:
				block["text"] = cleaned
				patched += 1

	try:
		choices = (
			response.get("choices")
			if isinstance(response, dict)
			else getattr(response, "choices", None)
		)
	except Exception:
		choices = None

	if isinstance(choices, list):
		for choice in choices:
			try:
				message = (
					choice.get("message")
					if isinstance(choice, dict)
					else getattr(choice, "message", None)
				)
			except Exception:
				message = None
			if message is None:
				continue
			try:
				text = (
					message.get("content")
					if isinstance(message, dict)
					else getattr(message, "content", None)
				)
			except Exception:
				text = None
			if not isinstance(text, str):
				continue
			cleaned, changed = _strip_think_tag_artifacts(text)
			if not changed:
				continue
			if isinstance(message, dict):
				message["content"] = cleaned
			else:
				try:
					message.content = cleaned
				except Exception:
					continue
			patched += 1

	return patched


def _drop_header(headers: dict[str, Any], name: str) -> None:
	for key in list(headers):
		if isinstance(key, str) and key.lower() == name.lower():
			headers.pop(key, None)


_CLIENT_AUTH_HEADER_NAMES = (
	"authorization",
	"x-api-key",
	"x-litellm-api-key",
	"anthropic-version",
	"anthropic-beta",
)


def _strip_client_auth_headers(data: dict[str, Any]) -> None:
	"""Remove client/proxy auth headers before non-Anthropic provider calls."""
	for header_key in ("headers", "extra_headers"):
		headers = data.get(header_key)
		if isinstance(headers, dict):
			for header_name in _CLIENT_AUTH_HEADER_NAMES:
				_drop_header(headers, header_name)

	provider_specific_header = data.get("provider_specific_header")
	if isinstance(provider_specific_header, dict):
		extra_headers = provider_specific_header.get("extra_headers")
		if isinstance(extra_headers, dict):
			for header_name in _CLIENT_AUTH_HEADER_NAMES:
				_drop_header(extra_headers, header_name)

	proxy_request = data.get("proxy_server_request")
	if isinstance(proxy_request, dict):
		headers = proxy_request.get("headers")
		if isinstance(headers, dict):
			for header_name in _CLIENT_AUTH_HEADER_NAMES:
				_drop_header(headers, header_name)


def _set_anthropic_oauth_headers(data: dict[str, Any], auth_header: str) -> None:
	"""Force Anthropic calls to use the live OAuth bearer, not client auth."""
	for header_key in ("headers", "extra_headers"):
		headers = data.get(header_key)
		if not isinstance(headers, dict):
			headers = {}
			data[header_key] = headers
		_drop_header(headers, "authorization")
		_drop_header(headers, "x-api-key")
		headers["authorization"] = auth_header

	provider_specific_header = data.setdefault("provider_specific_header", {})
	if isinstance(provider_specific_header, dict):
		provider_specific_header["custom_llm_provider"] = "anthropic"
		extra_headers = provider_specific_header.setdefault("extra_headers", {})
		if isinstance(extra_headers, dict):
			_drop_header(extra_headers, "authorization")
			_drop_header(extra_headers, "x-api-key")
			extra_headers["authorization"] = auth_header

	proxy_request = data.get("proxy_server_request")
	if isinstance(proxy_request, dict) and isinstance(proxy_request.get("headers"), dict):
		_drop_header(proxy_request["headers"], "authorization")
		_drop_header(proxy_request["headers"], "x-api-key")


def forward_provider_oauth(
	data: dict[str, Any], user_api_key_dict: dict[str, Any], **kwargs: Any
) -> dict[str, Any]:
	"""Inject provider oauth headers in the shape LiteLLM's pass-through expects.

	Upstream ccproxy's forward_oauth hook sets only extra_headers. LiteLLM's
	Anthropic /v1/messages path drops those headers unless custom_llm_provider
	is also set on provider_specific_header.

	This callback handles both ccproxy-routed requests AND direct model requests
	for Claude (Anthropic) models that use OAuth instead of API keys.
	"""

	metadata = data.get("metadata", {})
	if not isinstance(metadata, dict):
		metadata = {}
	model_config = metadata.get("ccproxy_model_config") or {}
	if not isinstance(model_config, dict):
		model_config = {}

	litellm_params = model_config.get("litellm_params", {})
	if not isinstance(litellm_params, dict):
		litellm_params = {}

	# Strip null bytes from any persisted payload field before downstream
	# spend-log persistence (Prisma -> Postgres `text`) rejects them with
	# SQLSTATE 22P05. Runs for every model, regardless of OAuth routing.
	if isinstance(data.get("messages"), list):
		data["messages"] = _strip_null_bytes(data["messages"])
	if "system" in data:
		data["system"] = _strip_null_bytes(data["system"])
	if "tools" in data:
		data["tools"] = _strip_null_bytes(data["tools"])

	# LiteLLM's spend-log builder serializes `litellm_params.proxy_server_request.body`
	# as a separate JSON column (spend_tracking_utils._get_proxy_server_request_for_spend_logs_payload).
	# Scrubbing `messages` alone leaves null bytes in the raw HTTP body field,
	# which still trips Postgres SQLSTATE 22P05 on insert.
	proxy_request = data.get("proxy_server_request")
	if isinstance(proxy_request, dict):
		if "body" in proxy_request:
			proxy_request["body"] = _strip_null_bytes(proxy_request["body"])
		if "headers" in proxy_request:
			proxy_request["headers"] = _strip_null_bytes(proxy_request["headers"])

	if _normalize_claude_metadata_model_group(data):
		metadata = data.get("metadata", {})
		if not isinstance(metadata, dict):
			metadata = {}

	# Always resolve the provider based on the actual model LiteLLM is about to call (crucial for fallbacks)
	routed_model = data.get("model") or metadata.get("ccproxy_litellm_model") or ""
	if not routed_model:
		return data

	provider_name = None
	alias_map = _load_model_name_to_provider_map()
	provider_name = alias_map.get(routed_model)

	if not provider_name:
		if routed_model.startswith("claude-"):
			provider_name = "anthropic"
		elif metadata.get("ccproxy_model_config"):
			# Fallback for ccproxy-routed requests where model might not be in alias map
			try:
				_, provider_name, _, _ = get_llm_provider(
					model=routed_model,
					custom_llm_provider=litellm_params.get("custom_llm_provider"),
					api_base=litellm_params.get("api_base"),
				)
			except Exception:
				pass
		if not provider_name:
			try:
				_, provider_name, _, _ = get_llm_provider(model=routed_model)
			except Exception:
				provider_name = None

	provider_name = _coerce_anthropic_provider_for_sanitization(
		data, routed_model, provider_name
	)

	if not provider_name:
		return data

	_sanitize_model_call_payload(data, provider_name)

	# Validate tool calls in conversation history against the request's tool
	# schemas. Mistral/Kimi/GLM/Scaleway-Devstral routinely emit empty-input or
	# missing-required-field tool_use blocks; if we resend them verbatim, the
	# model latches onto its own bad pattern and the agent loops on
	# tool_use_error. Skip for anthropic upstream where Claude's own tool-use
	# blocks are well-formed and may carry provider-internal state we must not
	# rewrite.
	if provider_name != "anthropic":
		try:
			required_map = _required_fields_by_tool(data)
			if required_map:
				sanitized, patched = _sanitize_history_tool_calls(
					data.get("messages"), required_map
				)
				if patched:
					data["messages"] = sanitized
					_TOOL_VALIDATION_LOGGER.info(
						"history tool-call cleanup: patched=%d model=%s provider=%s",
						patched,
						routed_model,
						provider_name,
					)
		except Exception as exc:  # noqa: BLE001 -- never block routing on validator
			_TOOL_VALIDATION_LOGGER.debug(
				"history tool-call cleanup raised: %s", exc, exc_info=True
			)

	# OAuth injection only applies to Anthropic provider. Other providers
	# (scaleway, mistral, ...) authenticate via their own `api_key` already
	# wired in litellm.yaml -- bail out before mutating headers.
	if provider_name != "anthropic":
		# FINAL safety clamp for all non-Anthropic providers
		for token_field in ("max_completion_tokens", "max_tokens"):
			if token_field in data:
				value = data[token_field]
				if isinstance(value, (int, float)) and value < 1:
					data[token_field] = 1024
					_TOOL_VALIDATION_LOGGER.error(
						"FINAL CLAMP: %s=%s for provider=%s", token_field, value, provider_name
					)
		return data

	request = data.get("proxy_server_request") or {}
	headers = request.get("headers", {}) if isinstance(request, dict) else {}
	oauth_token = _get_live_oauth_token(provider_name)
	if not oauth_token:
		return data
	auth_header = (
		oauth_token if oauth_token.startswith("Bearer ") else f"Bearer {oauth_token}"
	)

	config = get_config()
	_set_anthropic_oauth_headers(data, auth_header)
	extra_headers = data["provider_specific_header"]["extra_headers"]

	custom_user_agent = config.get_oauth_user_agent(provider_name)
	if custom_user_agent:
		extra_headers["user-agent"] = custom_user_agent
	elif headers.get("user-agent"):
		extra_headers.setdefault("user-agent", headers["user-agent"])

	return data


# ---------------------------------------------------------------------------
# Token reduction
#
# Every lever here is opt-in via env and must be *deterministic*: the Anthropic
# cache is a pure prefix match, so a suffix or a tool schema that varies between
# requests turns a 0.1x cache read into a 1.0x read plus a 1.25x write. Constant
# in, constant out.
# ---------------------------------------------------------------------------

# Claude Code's own utility prompts. Matched case-insensitively against the
# request text. These are background calls (titles, topic detection, summaries)
# that never need a frontier model.
_UTILITY_PROMPT_MARKERS = (
	"generate a concise title",
	"write a 5-10 word title",
	"generate a title for this conversation",
	"summarize this conversation",
	"analyze if this message indicates a new conversation topic",
	"extract any file paths that this command reads or modifies",
)

_UTILITY_SMALL_REQUEST_CHARS = 2000

_TERSE_DIRECTIVE = (
	"\n\nOutput budget: answer in the fewest words that fully resolve the request. "
	"No preamble, no recap, no narration of your own process, no summary of code "
	"you just wrote. Prefer the smallest correct diff and reuse what already "
	"exists over writing new code."
)


def _cheap_utility_model() -> str:
	return os.environ.get("CCPROXY_CHEAP_MODEL", "").strip()


def _utility_max_tokens() -> int:
	try:
		return max(1, int(os.environ.get("CCPROXY_UTILITY_MAX_TOKENS", "512")))
	except ValueError:
		return 512


def _terse_mode_enabled() -> bool:
	return os.environ.get("CCPROXY_TERSE_MODE", "").lower() in {"1", "true", "yes", "on"}


def _tool_description_limit() -> int:
	try:
		return max(0, int(os.environ.get("CCPROXY_TOOL_DESC_MAX_CHARS", "0")))
	except ValueError:
		return 0


def _request_text(data: dict[str, Any]) -> str:
	"""Flatten system + messages into one lowercase string for marker matching."""
	parts: list[str] = []
	system = data.get("system")
	if isinstance(system, str):
		parts.append(system)
	elif isinstance(system, list):
		parts.append(_system_message_text(system))
	messages = data.get("messages")
	if isinstance(messages, list):
		for message in messages:
			if isinstance(message, dict):
				parts.append(_system_message_text(message.get("content")))
	return "\n".join(parts).lower()


def _is_utility_request(data: dict[str, Any]) -> bool:
	"""True for Claude Code background calls that do not need a frontier model.

	Deliberately conservative: a false positive downgrades a real coding turn to
	a cheap model, which is far worse than missing a saving. Requires either an
	explicit Claude Code utility marker, or a tool-free Haiku-tier request small
	enough that it cannot be a real agentic turn.
	"""
	if data.get("tools"):
		return False
	text = _request_text(data)
	if any(marker in text for marker in _UTILITY_PROMPT_MARKERS):
		return True
	model = str(data.get("model") or "").lower()
	return "haiku" in model and len(text) < _UTILITY_SMALL_REQUEST_CHARS


def _route_utility_request(data: dict[str, Any]) -> bool:
	"""Send Claude Code utility calls to a cheap model. Returns True if rerouted.

	Only utility calls are rerouted. The main session is never downgraded
	mid-conversation: each model has its own prompt cache, so switching models
	100k tokens in costs more than letting the expensive model answer.
	"""
	cheap_model = _cheap_utility_model()
	if not cheap_model or not _is_utility_request(data):
		return False
	original = data.get("model")
	if original == cheap_model:
		return False
	data["model"] = cheap_model
	data["max_tokens"] = min(
		_utility_max_tokens(),
		data.get("max_tokens") or _utility_max_tokens(),
	)
	data.pop("max_completion_tokens", None)
	# Utility replies are short; streaming only adds chunk overhead.
	data["stream"] = False
	# Reasoning on a title generator is pure waste.
	data.pop("thinking", None)
	data["reasoning_effort"] = "low"
	metadata = data.get("metadata")
	if not isinstance(metadata, dict):
		metadata = {}
		data["metadata"] = metadata
	metadata["ccproxy_utility_route"] = True
	_TOOL_VALIDATION_LOGGER.info(
		"ccproxy utility reroute: %s -> %s max_tokens=%s",
		original,
		cheap_model,
		data["max_tokens"],
	)
	return True


def _inject_terse_directive(data: dict[str, Any]) -> None:
	"""Append a constant terseness directive to the system prompt.

	Output tokens cost ~5x input *and* become input on every later turn of the
	session, so trimming generated volume compounds. The directive is a fixed
	string appended at a fixed position: it shifts the cached prefix exactly
	once, then stays byte-stable.
	"""
	if not _terse_mode_enabled():
		return

	system = data.get("system")
	if isinstance(system, str):
		if _TERSE_DIRECTIVE not in system:
			data["system"] = system + _TERSE_DIRECTIVE
		return
	if isinstance(system, list):
		for block in reversed(system):
			if isinstance(block, dict) and block.get("type") == "text":
				text = block.get("text")
				if isinstance(text, str) and _TERSE_DIRECTIVE not in text:
					block["text"] = text + _TERSE_DIRECTIVE
				return

	# Responses API (Codex): budget lives in top-level `instructions`. Create it
	# when absent so a Responses request with only `input` still gets trimmed.
	# Native chatgpt/* is exempt: mutating the official Codex system prompt
	# splits the server-side prompt cache and risks backend behavior drift.
	if _is_chatgpt_native_responses_request(data):
		return
	if _request_is_responses_shape(data):
		instructions = data.get("instructions")
		if isinstance(instructions, str):
			if _TERSE_DIRECTIVE not in instructions:
				data["instructions"] = instructions + _TERSE_DIRECTIVE
		elif instructions is None:
			data["instructions"] = _TERSE_DIRECTIVE
		return

	messages = data.get("messages")
	if not isinstance(messages, list):
		return
	for message in messages:
		if not isinstance(message, dict) or message.get("role") not in {
			"system",
			"developer",
		}:
			continue
		content = message.get("content")
		if isinstance(content, str):
			if _TERSE_DIRECTIVE not in content:
				message["content"] = content + _TERSE_DIRECTIVE
			return
		if isinstance(content, list):
			for block in reversed(content):
				if isinstance(block, dict) and block.get("type") == "text":
					text = block.get("text")
					if isinstance(text, str) and _TERSE_DIRECTIVE not in text:
						block["text"] = text + _TERSE_DIRECTIVE
					return
			return


def _truncate_description(value: Any, limit: int) -> Any:
	if not isinstance(value, str) or len(value) <= limit:
		return value
	return value[:limit].rstrip() + "…"


def _minify_tool_schema(schema: Any, limit: int) -> Any:
	"""Trim verbose prose out of a JSON schema. Structure is never touched.

	Only `description` strings are shortened and `examples` dropped. Types,
	names, `required`, `enum` and nesting stay intact, so tool calls cannot
	break on a truncated schema.
	"""
	if isinstance(schema, list):
		return [_minify_tool_schema(item, limit) for item in schema]
	if not isinstance(schema, dict):
		return schema
	cleaned: dict[str, Any] = {}
	for key, value in schema.items():
		if key == "examples":
			continue
		if key == "description":
			cleaned[key] = _truncate_description(value, limit)
			continue
		cleaned[key] = _minify_tool_schema(value, limit)
	return cleaned


def _minify_tool_payload(data: dict[str, Any]) -> None:
	"""Shrink tool definitions in place, deterministically.

	Tool schemas sit at the very front of the Anthropic cache prefix
	(`tools -> system -> messages`), so this must produce identical bytes for
	identical input or it invalidates the whole prefix on every request. Pure
	string truncation at a fixed limit satisfies that; tool *order* is left
	exactly as the client sent it, since reordering would also bust the cache.
	"""
	limit = _tool_description_limit()
	if limit <= 0:
		return
	tools = data.get("tools")
	if not isinstance(tools, list):
		return
	minified: list[Any] = []
	for tool in tools:
		if not isinstance(tool, dict):
			minified.append(tool)
			continue
		entry = dict(tool)
		if "description" in entry:
			entry["description"] = _truncate_description(entry["description"], limit)
		for schema_key in ("input_schema", "parameters"):
			if isinstance(entry.get(schema_key), dict):
				entry[schema_key] = _minify_tool_schema(entry[schema_key], limit)
		function = entry.get("function")
		if isinstance(function, dict):
			function = dict(function)
			if "description" in function:
				function["description"] = _truncate_description(
					function["description"], limit
				)
			if isinstance(function.get("parameters"), dict):
				function["parameters"] = _minify_tool_schema(
					function["parameters"], limit
				)
			entry["function"] = function
		minified.append(entry)
	data["tools"] = minified


def _usage_value(usage: Any, *names: str) -> int:
	for name in names:
		value = None
		if isinstance(usage, dict):
			value = usage.get(name)
		else:
			value = getattr(usage, name, None)
		if isinstance(value, (int, float)):
			return int(value)
	return 0


def _log_cache_usage(kwargs: dict[str, Any], response_obj: Any) -> None:
	"""Emit prompt-cache hit/write counts so the saving is measurable.

	Without this every caching decision is guesswork. Providers report it under
	different names: Anthropic `cache_read_input_tokens`, OpenAI
	`cached_tokens`, DeepSeek `prompt_cache_hit_tokens`.
	"""
	model = kwargs.get("model", "unknown")
	usage = getattr(response_obj, "usage", None)
	if usage is None and isinstance(response_obj, dict):
		usage = response_obj.get("usage")
	if usage is None:
		return
	details = None
	if isinstance(usage, dict):
		details = usage.get("prompt_tokens_details")
	else:
		details = getattr(usage, "prompt_tokens_details", None)

	cache_read = _usage_value(
		usage, "cache_read_input_tokens", "prompt_cache_hit_tokens"
	) or _usage_value(details, "cached_tokens")
	cache_write = _usage_value(
		usage, "cache_creation_input_tokens", "prompt_cache_miss_tokens"
	)
	prompt_tokens = _usage_value(usage, "prompt_tokens", "input_tokens")
	completion_tokens = _usage_value(usage, "completion_tokens", "output_tokens")
	billable = prompt_tokens or (cache_read + cache_write)
	hit_rate = (cache_read / billable * 100.0) if billable else 0.0

	model = kwargs.get("model", "unknown")
	_TOOL_VALIDATION_LOGGER.info(
		"ccproxy cache usage: model=%s prompt=%d completion=%d "
		"cache_read=%d cache_write=%d hit_rate=%.1f%%",
		model,
		prompt_tokens,
		completion_tokens,
		cache_read,
		cache_write,
		hit_rate,
	)
	try:
		import json as _json
		import os as _os
		from datetime import datetime as _dt

		path = _os.environ.get(
			"CLAUDE_COMPRESSION_SAVINGS_LOG", "/tmp/token_savings.jsonl"
		)
		_os.makedirs(_os.path.dirname(path) or ".", exist_ok=True)
		entry = {
			"kind": "cache",
			"timestamp": _dt.utcnow().isoformat() + "Z",
			"model": model,
			"cache_read": cache_read,
			"cache_write": cache_write,
			"prompt_tokens": prompt_tokens,
			"completion_tokens": completion_tokens,
		}
		with open(path, "a", encoding="utf-8") as fh:
			fh.write(_json.dumps(entry) + "\n")
	except Exception:
		pass


class _ValidatingCCProxyHandler(CCProxyHandler):
	"""`CCProxyHandler` with response-side tool-call validation.

	The parent runs all the `self.hooks` registered by ccproxy (including
	`forward_provider_oauth`) inside `async_pre_call_hook`. This subclass
	adds `async_post_call_success_hook` so we can also scrub malformed
	tool calls out of the **model's reply** before it reaches the agent
	(Claude Code). Pre-call history cleanup already runs inside
	`forward_provider_oauth`; the two passes complement each other.

	Streaming responses bypass this hook -- LiteLLM uses
	`async_post_call_streaming_iterator_hook` for those. Best-effort: the
	history cleanup pass on the *next* request will still catch the bad
	call once it lands in the conversation, breaking the loop one turn
	later than ideal.
	"""

	async def async_pre_call_hook(self, *args: Any, **kwargs: Any) -> Any:
		"""Normalize Claude primary routes for both LiteLLM and ccproxy hook APIs."""
		data: dict[str, Any] | None = None
		user_api_key_dict: Any = None
		official_proxy_signature = False

		if len(args) >= 3 and isinstance(args[2], dict):
			# LiteLLM proxy CustomLogger API:
			# async_pre_call_hook(user_api_key_dict, cache, data, call_type)
			user_api_key_dict = args[0]
			data = args[2]
			official_proxy_signature = True
		elif args and isinstance(args[0], dict):
			# ccproxy parent API: async_pre_call_hook(data, user_api_key_dict, **kwargs)
			data = args[0]
			user_api_key_dict = args[1] if len(args) > 1 else {}
		elif isinstance(kwargs.get("data"), dict):
			data = kwargs["data"]
			user_api_key_dict = kwargs.get("user_api_key_dict", {})
			official_proxy_signature = True

		if data is None:
			parent_hook = getattr(CCProxyHandler, "async_pre_call_hook", None)
			if parent_hook is None:
				return None
			return await parent_hook(self, *args, **kwargs)

		requested_model = data.get("model")
		metadata = data.get("metadata")
		if not isinstance(metadata, dict):
			metadata = {}
			data["metadata"] = metadata
		metadata.setdefault("ccproxy_requested_model", requested_model)
		metadata.setdefault("ccproxy_root_request_id", uuid.uuid4().hex)
		metadata["ccproxy_tool_search_cohort"] = _sticky_experiment_cohort(data, "TOOL_SEARCH")
		# Stamp Responses shape now: the /v1/responses->chat bridge can rewrite
		# `data` to carry `messages` before the post-call/stream hooks run, which
		# would flip `_request_is_responses_shape` to False and let Claude repair
		# passes hit a bridged Codex reply. The stamp survives that rewrite.
		if _request_is_responses_shape(data):
			metadata = data.get("metadata")
			if not isinstance(metadata, dict):
				metadata = {}
				data["metadata"] = metadata
			metadata["ccproxy_responses_shape"] = True
		_minify_tool_payload(data)
		_inject_terse_directive(data)
		if _terse_mode_enabled() and not _is_chatgpt_native_responses_request(data):
			metadata["ccproxy_terse_applied"] = True
		if _route_utility_request(data):
			requested_model = data["model"]
		_normalize_claude_metadata_model_group(data)
		_force_claude_primary_passthrough(data, requested_model)
		if _is_chatgpt_native_responses_request(data):
			# Tool-bearing Codex/ChatGPT Responses traffic must not silently
			# continue through a different provider's chat adapter. A fallback
			# cannot preserve Responses call IDs and tool semantics reliably.
			data["disable_fallbacks"] = True
		_emit_savings_event(data, "accepted")

		if official_proxy_signature:
			forward_kwargs = dict(kwargs)
			for key in ("data", "user_api_key_dict", "cache", "call_type"):
				forward_kwargs.pop(key, None)
			data = forward_provider_oauth(
				data, user_api_key_dict or {}, **forward_kwargs
			)
			return data

		parent_hook = getattr(CCProxyHandler, "async_pre_call_hook", None)
		if parent_hook is not None:
			parent_kwargs = dict(kwargs)
			for key in ("data", "user_api_key_dict", "cache", "call_type"):
				parent_kwargs.pop(key, None)
			data = await parent_hook(self, data, user_api_key_dict, **parent_kwargs)

		if _force_claude_primary_passthrough(data, requested_model):
			_TOOL_VALIDATION_LOGGER.info(
				"claude primary passthrough restored after ccproxy routing: model=%s",
				requested_model,
			)

		# Tool-search must be the LAST mutation of tools[] so nothing reorders
		# the breakpoint tool afterwards.
		_apply_tool_search_defer(data)

		return data

	async def async_pre_call_deployment_hook(
		self,
		kwargs: dict[str, Any],
		call_type: Any,
	) -> dict[str, Any] | None:
		"""Scrub the final provider payload after LiteLLM picks a deployment.

		LiteLLM can convert Anthropic `/v1/messages` requests into chat payloads
		after `async_pre_call_hook` has already run. When fallback then selects a
		non-Anthropic provider, Claude `thinking_blocks` can leak into Mistral or
		OpenAI-compatible APIs. This deployment hook runs late enough to see the
		actual provider payload for every fallback attempt.
		"""
		# ULTIMATE safety clamp - last chance before request is sent
		for token_field in ("max_completion_tokens", "max_tokens"):
			if token_field in kwargs:
				value = kwargs[token_field]
				if isinstance(value, (int, float)) and value < 1:
					kwargs[token_field] = 1024
					_TOOL_VALIDATION_LOGGER.error(
						"ULTIMATE CLAMP: %s=%s for kwargs", token_field, value
					)
		provider_name = _provider_for_model_call(kwargs)
		if not provider_name:
			return kwargs
		metadata = kwargs.get("metadata")
		if not isinstance(metadata, dict):
			metadata = {}
		if (
			provider_name != "anthropic"
			and metadata.get("ccproxy_provider_model")
			in _CLAUDE_PRIMARY_PROVIDER_MODEL_VALUES
		):
			kwargs.pop("api_key", None)
			kwargs.pop("api_base", None)
		if provider_name != "anthropic":
			_strip_client_auth_headers(kwargs)
			_strip_tool_search_artifacts(kwargs)
		cleaned = _sanitize_model_call_payload(kwargs, provider_name)
		if provider_name == "anthropic":
			oauth_token = _get_live_oauth_token(provider_name)
			if oauth_token:
				auth_header = (
					oauth_token
					if oauth_token.startswith("Bearer ")
					else f"Bearer {oauth_token}"
				)
				_set_anthropic_oauth_headers(cleaned, auth_header)
		return cleaned

	async def async_log_success_event(
		self,
		kwargs: dict[str, Any],
		response_obj: Any,
		start_time: Any,
		end_time: Any,
	) -> None:
		"""Log prompt-cache usage. Fires for streaming and non-streaming alike."""
		try:
			_log_cache_usage(kwargs, response_obj)
			usage = getattr(response_obj, "usage", None)
			if usage is None and isinstance(response_obj, dict):
				usage = response_obj.get("usage")
			_emit_savings_event(
				kwargs,
				"success",
				input_tokens=_usage_value(usage, "prompt_tokens", "input_tokens"),
				output_tokens=_usage_value(usage, "completion_tokens", "output_tokens"),
			)
		except Exception:
			_TOOL_VALIDATION_LOGGER.debug("cache usage logging failed", exc_info=True)

	async def async_log_failure_event(
		self,
		kwargs: dict[str, Any],
		response_obj: Any,
		start_time: Any,
		end_time: Any,
	) -> None:
		if response_obj is None:
			_emit_savings_event(kwargs, "failed_unknown")
			_TOOL_VALIDATION_LOGGER.debug(
				"ccproxy ignored empty failure callback: model=%s duration_ms=%s",
				kwargs.get("model", "unknown"),
				_duration_ms(start_time, end_time) or "unknown",
			)
			return
		metadata = kwargs.get("metadata")
		if not isinstance(metadata, dict):
			metadata = {}
		duration = _duration_ms(start_time, end_time)
		_TOOL_VALIDATION_LOGGER.error(
			"ccproxy request failed: model_name=%s model=%s error_type=%s duration_ms=%s error=%s",
			metadata.get("ccproxy_model_name", "unknown"),
			kwargs.get("model", "unknown"),
			type(response_obj).__name__,
			duration if duration is not None else "unknown",
			_failure_message(response_obj) or "unknown",
		)
		_emit_savings_event(
			kwargs,
			"provider_error",
			duration_ms=duration,
			error_type=type(response_obj).__name__,
		)

	async def async_post_call_success_hook(
		self,
		data: dict[str, Any],
		user_api_key_dict: Any,
		response: Any,
	) -> Any:
		if _response_repair_should_bypass(data):
			_TOOL_VALIDATION_LOGGER.debug(
				"Responses-shape reply bypasses Claude textual tool recovery: model=%s",
				data.get("model"),
			)
			parent_hook = getattr(
				CCProxyHandler, "async_post_call_success_hook", None
			)
			if parent_hook is not None and parent_hook is not type(self).async_post_call_success_hook:
				return await parent_hook(self, data, user_api_key_dict, response)
			return response
		try:
			native_tool_patched = _convert_native_text_tool_calls(response, data)
			if native_tool_patched:
				_TOOL_VALIDATION_LOGGER.info(
					"response native tool-call conversion: patched=%d model=%s",
					native_tool_patched,
					data.get("model"),
				)
			planned_read_patched = _convert_planned_read_tool(response, data)
			if planned_read_patched:
				_TOOL_VALIDATION_LOGGER.info(
					"response planned-read tool conversion: patched=%d model=%s",
					planned_read_patched,
					data.get("model"),
				)
			planned_agent_patched = _convert_planned_agent_tool(response, data)
			if planned_agent_patched:
				_TOOL_VALIDATION_LOGGER.info(
					"response planned-agent tool conversion: patched=%d model=%s",
					planned_agent_patched,
					data.get("model"),
				)
			planned_agent_final_patched = _repair_planned_agent_final_text(response, data)
			if planned_agent_final_patched:
				_TOOL_VALIDATION_LOGGER.info(
					"response planned-agent final repair: patched=%d model=%s",
					planned_agent_final_patched,
					data.get("model"),
				)
			command_tool_patched = _convert_single_tool_text_command(response, data)
			if command_tool_patched:
				_TOOL_VALIDATION_LOGGER.info(
					"response bare-command tool conversion: patched=%d model=%s",
					command_tool_patched,
					data.get("model"),
				)
			planned_tool_patched = _rewrite_planned_tool_call(response, data)
			if planned_tool_patched:
				_TOOL_VALIDATION_LOGGER.info(
					"response planned-tool rewrite: patched=%d model=%s",
					planned_tool_patched,
					data.get("model"),
				)
			text_patched = _sanitize_response_text_artifacts(response)
			if text_patched:
				_TOOL_VALIDATION_LOGGER.info(
					"response think-tag cleanup: patched=%d model=%s",
					text_patched,
					data.get("model"),
				)
			required_map = _required_fields_by_tool(data)
			if required_map:
				patched = _sanitize_response_tool_calls(response, required_map)
				if patched:
					_TOOL_VALIDATION_LOGGER.info(
						"response tool-call validation: patched=%d model=%s",
						patched,
						data.get("model"),
					)
		except Exception as exc:  # noqa: BLE001 -- never break the response
			_TOOL_VALIDATION_LOGGER.debug(
				"response tool-call validation raised: %s", exc, exc_info=True
			)

		parent_hook = getattr(
			CCProxyHandler, "async_post_call_success_hook", None
		)
		if parent_hook is not None and parent_hook is not type(self).async_post_call_success_hook:
			return await parent_hook(self, data, user_api_key_dict, response)
		return response

	async def async_post_call_streaming_iterator_hook(
		self,
		user_api_key_dict: Any,
		response: Any,
		request_data: dict[str, Any],
	):
		if _response_repair_should_bypass(request_data):
			_TOOL_VALIDATION_LOGGER.debug(
				"Responses-shape stream bypasses legacy buffering/recovery: model=%s",
				request_data.get("model"),
			)
			async for item in response:
				yield item
			return
		if not _is_anthropic_messages_stream(request_data):
			async for item in response:
				yield item
			return

		if not _should_buffer_tool_stream(request_data):
			sanitizer = _AnthropicSSEStreamSanitizer()
			async for item in response:
				for sanitized_item in sanitizer.feed(item):
					yield sanitized_item
			for sanitized_item in sanitizer.flush():
				yield sanitized_item
			if sanitizer.dropped_thinking_deltas:
				_TOOL_VALIDATION_LOGGER.info(
					"stream thinking-delta validation: dropped=%d model=%s",
					sanitizer.dropped_thinking_deltas,
					request_data.get("model"),
				)
			return

		buffered: list[Any] = []
		buffer_timeout = _stream_buffer_timeout_seconds()
		iterator = response.__aiter__()
		while True:
			pending_item: asyncio.Future[Any] | None = None
			try:
				if buffer_timeout <= 0:
					item = await iterator.__anext__()
				else:
						pending_item = asyncio.ensure_future(iterator.__anext__())
						while True:
							done, _ = await asyncio.wait(
								{pending_item},
								timeout=buffer_timeout,
								return_when=asyncio.FIRST_COMPLETED,
							)
							if done:
								done_item = pending_item
								pending_item = None
								item = await done_item
								break
							try:
								tool_events = _streamed_planned_tool_rewrite_events(
									buffered, request_data
								)
								if tool_events is None:
									tool_events = _streamed_native_tool_events([], request_data)
								if tool_events is None:
									tool_events = _streamed_command_tool_events([], request_data)
							except Exception as exc:  # noqa: BLE001 -- never break streaming fallback
								_TOOL_VALIDATION_LOGGER.debug(
									"stream timeout tool conversion raised: %s",
									exc,
									exc_info=True,
								)
								tool_events = None
							if tool_events:
								_TOOL_VALIDATION_LOGGER.info(
									"stream timeout planned-tool conversion: patched=%d model=%s timeout=%s buffered=%d",
									1,
									request_data.get("model"),
									buffer_timeout,
									len(buffered),
								)
								pending_item.cancel()
								pending_item = None
								for event in tool_events:
									yield event
								return
							if buffered:
								_TOOL_VALIDATION_LOGGER.debug(
									"stream buffer timeout without planned tool; sending keepalive: model=%s timeout=%s buffered=%d",
									request_data.get("model"),
									buffer_timeout,
									len(buffered),
								)
							else:
								_TOOL_VALIDATION_LOGGER.debug(
									"stream first-chunk timeout without planned tool; sending keepalive: model=%s timeout=%s",
									request_data.get("model"),
									buffer_timeout,
								)
							yield _anthropic_ping_event()
			except StopAsyncIteration:
				break
			finally:
				if pending_item is not None and not pending_item.done():
					pending_item.cancel()
			buffered.append(item)

		try:
			planned_stream_patched = False
			tool_events = _streamed_planned_tool_rewrite_events(
				buffered, request_data
			)
			if tool_events:
				planned_stream_patched = True
				_TOOL_VALIDATION_LOGGER.info(
					"stream planned-tool rewrite: patched=%d model=%s",
					1,
					request_data.get("model"),
				)
			if tool_events is None:
				tool_events = _streamed_native_tool_events(buffered, request_data)
			if tool_events and not planned_stream_patched:
				_TOOL_VALIDATION_LOGGER.info(
					"stream native tool-call conversion: patched=%d model=%s",
					1,
					request_data.get("model"),
				)
			if tool_events is None:
				tool_events = _streamed_command_tool_events(buffered, request_data)
		except Exception as exc:  # noqa: BLE001 -- never break streaming fallback
			_TOOL_VALIDATION_LOGGER.debug(
				"stream command-tool conversion raised: %s", exc, exc_info=True
			)
			tool_events = None

		if tool_events:
			_TOOL_VALIDATION_LOGGER.info(
				"stream command-tool conversion: patched=%d model=%s",
				1,
				request_data.get("model"),
			)
			for event in tool_events:
				yield event
			return

		try:
			final_events = _streamed_planned_agent_final_events(buffered, request_data)
		except Exception as exc:  # noqa: BLE001 -- never break streaming fallback
			_TOOL_VALIDATION_LOGGER.debug(
				"stream planned-agent final repair raised: %s", exc, exc_info=True
			)
			final_events = None
		if final_events:
			_TOOL_VALIDATION_LOGGER.info(
				"stream planned-agent final repair: patched=%d model=%s",
				1,
				request_data.get("model"),
			)
			_log_tool_search_discovery(buffered, request_data)
			for event in final_events:
				yield event
			return

		sanitized_items, dropped = _sanitize_anthropic_sse_stream_items(buffered)
		if dropped:
			_TOOL_VALIDATION_LOGGER.info(
				"stream thinking-delta validation: dropped=%d model=%s",
				dropped,
				request_data.get("model"),
			)
		_log_tool_search_discovery(buffered, request_data)
		for item in sanitized_items:
			yield item


ccproxy_handler = _ValidatingCCProxyHandler()
