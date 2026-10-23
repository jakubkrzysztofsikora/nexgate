# ruff: noqa
from typing import Any
from datetime import datetime, timezone
import hashlib
import json
import os
import queue
import threading
from datetime import datetime, timezone
from ccshims import get_llm_provider
from ccshims.const import _ANTHROPIC_EMPTY_TEXT_PLACEHOLDER, _HARNESS_HEADER, _HARNESS_TOKEN, _KNOWN_HARNESSES, _LITELLM_CONFIG_PATH, _SAVINGS_EVENTS_ENABLED, _SAVINGS_EVENT_MAX_BYTES, _SAVINGS_EVENT_PATH, _SAVINGS_EVENT_QUEUE, _SAVINGS_EVENT_SALT, _SAVINGS_EVENT_SCHEMA, _TOOL_VALIDATION_LOGGER


_MODEL_NAME_MAP_MTIME: float | None = None

_MODEL_NAME_TO_PROVIDER: dict[str, str] = {}

_SAVINGS_EVENT_WRITER_STARTED = False

_SAVINGS_EVENT_DROPPED = 0

def _hash_telemetry_identifier(value: Any) -> str | None:
    """Return a stable, non-reversible identifier for correlation only."""
    if not isinstance(value, str) or not value:
        return None
    if not _SAVINGS_EVENTS_ENABLED:
        return None
    key = hashlib.sha256(_SAVINGS_EVENT_SALT.encode('utf-8')).digest()
    return hashlib.blake2s(value.encode('utf-8'), key=key, digest_size=12).hexdigest()

def _trusted_harness(data: dict[str, Any]) -> str:
    """Accept harness attribution only from the configured authenticated header."""
    request = data.get('proxy_server_request')
    headers = request.get('headers') if isinstance(request, dict) else None
    if not isinstance(headers, dict) or not _HARNESS_TOKEN:
        return 'unknown'
    value = next((v for k, v in headers.items() if str(k).lower() == _HARNESS_HEADER), None)
    if not isinstance(value, str):
        return 'unknown'
    harness, sep, token = value.partition(':')
    return harness if sep and token == _HARNESS_TOKEN and (harness in _KNOWN_HARNESSES) else 'unknown'

def _emit_savings_event(data: dict[str, Any], outcome: str, **extra: Any) -> None:
    """Persist a bounded lifecycle record; telemetry failure never affects a call."""
    global _SAVINGS_EVENT_DROPPED
    try:
        if not _SAVINGS_EVENTS_ENABLED:
            return
        metadata = data.get('metadata') if isinstance(data.get('metadata'), dict) else {}
        tools = data.get('tools')
        event = {'schema': _SAVINGS_EVENT_SCHEMA, 'timestamp': datetime.now(timezone.utc).isoformat(), 'outcome': outcome, 'harness': _trusted_harness(data), 'request_id': _hash_telemetry_identifier(metadata.get('request_id')), 'session_id': _hash_telemetry_identifier(metadata.get('session_id')), 'root_request_id': _hash_telemetry_identifier(metadata.get('ccproxy_root_request_id')), 'requested_model': str(metadata.get('ccproxy_requested_model', data.get('model', 'unknown')))[:160], 'resolved_model': str(data.get('model', 'unknown'))[:160], 'responses_shape': bool(metadata.get('ccproxy_responses_shape')), 'tool_count': len(tools) if isinstance(tools, list) else 0, 'techniques': {'tool_search': bool(metadata.get('ccproxy_tool_search_applied')), 'utility_route': bool(metadata.get('ccproxy_utility_route')), 'terse': bool(metadata.get('ccproxy_terse_applied'))}}
        event.update({k: v for k, v in extra.items() if v is not None and isinstance(v, (int, float, bool)) or (isinstance(v, str) and len(v) <= 160)})
        _SAVINGS_EVENT_QUEUE.put_nowait(json.dumps(event, separators=(',', ':')) + '\n')
    except queue.Full:
        _SAVINGS_EVENT_DROPPED += 1
        if _SAVINGS_EVENT_DROPPED == 1 or _SAVINGS_EVENT_DROPPED % 100 == 0:
            _TOOL_VALIDATION_LOGGER.warning('savings telemetry queue full; dropped=%d', _SAVINGS_EVENT_DROPPED)
    except Exception:
        _TOOL_VALIDATION_LOGGER.debug('savings event emission failed', exc_info=True)

def _savings_event_writer() -> None:
    """Single daemon writer keeps filesystem latency off LiteLLM async hooks."""
    while True:
        line = _SAVINGS_EVENT_QUEUE.get()
        try:
            os.makedirs(os.path.dirname(_SAVINGS_EVENT_PATH) or '.', exist_ok=True)
            if os.path.exists(_SAVINGS_EVENT_PATH) and os.path.getsize(_SAVINGS_EVENT_PATH) >= _SAVINGS_EVENT_MAX_BYTES:
                rotated = f"{_SAVINGS_EVENT_PATH}.{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
                os.replace(_SAVINGS_EVENT_PATH, rotated)
                _TOOL_VALIDATION_LOGGER.warning('rotated savings telemetry log: %s', rotated)
            with open(_SAVINGS_EVENT_PATH, 'a', encoding='utf-8') as fh:
                fh.write(line)
        except Exception:
            _TOOL_VALIDATION_LOGGER.warning('savings telemetry writer failed', exc_info=True)
        finally:
            _SAVINGS_EVENT_QUEUE.task_done()

def _start_savings_event_writer() -> None:
    global _SAVINGS_EVENT_WRITER_STARTED
    if _SAVINGS_EVENTS_ENABLED and (not _SAVINGS_EVENT_WRITER_STARTED):
        threading.Thread(target=_savings_event_writer, name='savings-event-writer', daemon=True).start()
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
        with open(_LITELLM_CONFIG_PATH, 'r', encoding='utf-8') as handle:
            config = yaml.safe_load(handle) or {}
    except Exception:
        return _MODEL_NAME_TO_PROVIDER
    new_map: dict[str, str] = {}
    for entry in config.get('model_list', []) or []:
        if not isinstance(entry, dict):
            continue
        model_name = entry.get('model_name')
        litellm_params = entry.get('litellm_params') or {}
        underlying = litellm_params.get('model') if isinstance(litellm_params, dict) else None
        if not isinstance(model_name, str) or not isinstance(underlying, str):
            continue
        try:
            _, provider, _, _ = get_llm_provider(model=underlying, custom_llm_provider=litellm_params.get('custom_llm_provider') if isinstance(litellm_params, dict) else None, api_base=litellm_params.get('api_base') if isinstance(litellm_params, dict) else None)
        except Exception:
            provider = None
        if provider:
            new_map[model_name] = provider
    _MODEL_NAME_TO_PROVIDER = new_map
    _MODEL_NAME_MAP_MTIME = mtime
    return _MODEL_NAME_TO_PROVIDER

_load_model_name_to_provider_map()

def _strip_null_bytes(value: Any) -> Any:
    """Recursively strip `\\u0000` from strings nested in dict/list/scalar.

	Postgres rejects `\\u0000` in `text`/`jsonb` (SQLSTATE 22P05). LiteLLM
	spend-log persistence runs the same `data["messages"]` through Prisma,
	so any client payload carrying stray null bytes (binary leak, encoded
	image fragment, malformed tool result) crashes the spend job.
	"""
    if isinstance(value, str):
        if '\x00' in value:
            return value.replace('\x00', '')
        return value
    if isinstance(value, dict):
        return {key: _strip_null_bytes(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_strip_null_bytes(item) for item in value]
    return value

def _sticky_experiment_cohort(data: dict[str, Any], experiment: str) -> str:
    """Return a stable control/treatment assignment for a known session only."""
    percentage = int(os.getenv(f'CCPROXY_{experiment}_TREATMENT_PERCENT', '0'))
    if percentage <= 0:
        return 'control'
    metadata = data.get('metadata')
    session_id = metadata.get('session_id') if isinstance(metadata, dict) else None
    if not isinstance(session_id, str) or not session_id:
        return 'unknown'
    bucket = int(hashlib.sha256(f'{experiment}:{session_id}'.encode()).hexdigest()[:8], 16) % 100
    return 'treatment' if bucket < min(percentage, 100) else 'control'

def _is_non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())

def _non_whitespace_text(value: Any) -> str:
    if isinstance(value, str) and value.strip():
        return value
    return _ANTHROPIC_EMPTY_TEXT_PLACEHOLDER

def _trust_request_model_group() -> bool:
    return os.getenv('CCPROXY_TRUST_REQUEST_MODEL_GROUP', '').lower() in {'1', 'true', 'yes', 'on'}
