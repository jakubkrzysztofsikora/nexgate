# ruff: noqa
from typing import Any
import json
import os
import subprocess
import threading
import time
from ccshims.tele import _trust_request_model_group
from ccshims.const import _ANTHROPIC_API_BASE, _CLAUDE_FALLBACK_MODEL_GROUPS, _CLAUDE_ONE_MILLION_CONTEXT_BETA, _CLAUDE_ONE_MILLION_CONTEXT_MODELS, _CLAUDE_PRIMARY_MODEL_GROUPS, _CLAUDE_PRIMARY_PROVIDER_MODELS, _TOOL_VALIDATION_LOGGER


def _get_oauth_refresh_ttl_seconds() -> float:
    raw_ttl = os.getenv('CCPROXY_OAUTH_REFRESH_SECONDS', '60')
    try:
        return max(0.0, float(raw_ttl))
    except ValueError:
        return 60.0

_OAUTH_REFRESH_TTL_SECONDS = _get_oauth_refresh_ttl_seconds()

def _get_anthropic_primary_probe_seconds() -> float:
    raw = os.getenv('ANTHROPIC_PRIMARY_PROBE_SECONDS', '60')
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 60.0

_ANTHROPIC_PRIMARY_PROBE_SECONDS = _get_anthropic_primary_probe_seconds()

def _get_anthropic_primary_cooldown_seconds() -> float:
    raw = os.getenv('ANTHROPIC_PRIMARY_COOLDOWN_SECONDS', '900')
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 900.0

_ANTHROPIC_PRIMARY_COOLDOWN_SECONDS = _get_anthropic_primary_cooldown_seconds()

_anthropic_primary_cooldown_until: float = 0.0

_OAUTH_TOKEN_CACHE: dict[str, tuple[str, float]] = {}

_anthropic_token_state_lock = threading.Lock()

_ANTHROPIC_FILE_TOKEN_CACHE: tuple[float, str, str | None, float] = (0.0, '', None, 0.0)

def _stream_buffer_timeout_seconds() -> float:
    raw_timeout = os.getenv('CCPROXY_STREAM_BUFFER_TIMEOUT_SECONDS', os.getenv('ANTHROPIC_MESSAGES_FIRST_CHUNK_TIMEOUT', '30'))
    try:
        return max(0.0, float(raw_timeout))
    except (TypeError, ValueError):
        return 30.0

def _anthropic_ping_event() -> str:
    return 'event: ping\ndata: {"type":"ping"}\n\n'

def _normalize_claude_metadata_model_group(data: dict[str, Any]) -> bool:
    """Keep Claude primary aliases from being silently re-routed by stale metadata."""
    if _trust_request_model_group():
        return False
    model = data.get('model')
    if model not in _CLAUDE_PRIMARY_MODEL_GROUPS:
        return False
    metadata = data.get('metadata')
    if not isinstance(metadata, dict):
        return False
    requested_group = metadata.get('model_group')
    if not isinstance(requested_group, str) or requested_group == model:
        return False
    metadata['model_group'] = model
    proxy_request = data.get('proxy_server_request')
    if isinstance(proxy_request, dict):
        body = proxy_request.get('body')
        if isinstance(body, dict):
            body_metadata = body.get('metadata')
            if isinstance(body_metadata, dict):
                body_metadata['model_group'] = model
    _TOOL_VALIDATION_LOGGER.info('claude metadata model_group normalization: requested=%s model=%s', requested_group, model)
    return True

def _force_claude_primary_passthrough(data: dict[str, Any], requested_model: Any | None=None) -> bool:
    """Keep Claude primary aliases on the requested LiteLLM model group."""
    if _trust_request_model_group():
        return False
    model = requested_model if requested_model is not None else data.get('model')
    if model not in _CLAUDE_PRIMARY_MODEL_GROUPS:
        return False
    provider_model = _CLAUDE_PRIMARY_PROVIDER_MODELS[model]
    metadata = data.get('metadata')
    if not isinstance(metadata, dict):
        metadata = {}
        data['metadata'] = metadata
    changed = data.get('model') != model
    data['model'] = model
    for key, value in {'ccproxy_alias_model': model, 'ccproxy_model_name': 'default', 'ccproxy_litellm_model': model, 'ccproxy_model_config': None, 'ccproxy_is_passthrough': True}.items():
        if metadata.get(key) != value:
            changed = True
            metadata[key] = value
    return changed

def _force_claude_primary_direct_provider(data: dict[str, Any], requested_model: Any | None=None) -> bool:
    if _trust_request_model_group():
        return False
    model = requested_model if requested_model is not None else data.get('model')
    if model not in _CLAUDE_PRIMARY_MODEL_GROUPS:
        return False
    provider_model = _CLAUDE_PRIMARY_PROVIDER_MODELS[model]
    metadata = data.get('metadata')
    if not isinstance(metadata, dict):
        metadata = {}
        data['metadata'] = metadata
    data['model'] = provider_model
    data['api_base'] = _ANTHROPIC_API_BASE
    if model in _CLAUDE_ONE_MILLION_CONTEXT_MODELS:
        extra_headers = data.get('extra_headers')
        if not isinstance(extra_headers, dict):
            extra_headers = {}
            data['extra_headers'] = extra_headers
        existing_beta = extra_headers.get('anthropic-beta')
        if isinstance(existing_beta, str) and existing_beta.strip():
            if _CLAUDE_ONE_MILLION_CONTEXT_BETA not in existing_beta:
                extra_headers['anthropic-beta'] = f'{existing_beta},{_CLAUDE_ONE_MILLION_CONTEXT_BETA}'
        else:
            extra_headers['anthropic-beta'] = _CLAUDE_ONE_MILLION_CONTEXT_BETA
    token = _get_live_oauth_token('anthropic')
    if token:
        data['api_key'] = token
    metadata['model_group'] = model
    metadata['ccproxy_alias_model'] = model
    metadata['ccproxy_model_name'] = 'default'
    metadata['ccproxy_litellm_model'] = model
    metadata['ccproxy_provider_model'] = provider_model
    metadata['ccproxy_model_config'] = None
    metadata['ccproxy_is_passthrough'] = True
    return True

def _get_source_command(source: Any) -> str:
    if isinstance(source, str):
        return source
    if isinstance(source, dict):
        return str(source.get('command', ''))
    return str(getattr(source, 'command', ''))

def _get_live_oauth_token(provider_name: str) -> str | None:
    global _anthropic_primary_cooldown_until, _ANTHROPIC_FILE_TOKEN_CACHE
    from ccproxy.config import get_config
    file_token = None
    token_file = os.getenv('CCPROXY_ANTHROPIC_OAUTH_TOKEN_FILE') if provider_name == 'anthropic' else None
    if token_file:
        if os.path.exists(token_file):
            try:
                mtime = os.path.getmtime(token_file)
                with _anthropic_token_state_lock:
                    cached_file = _ANTHROPIC_FILE_TOKEN_CACHE
                    if cached_file and cached_file[0] == mtime and (cached_file[1] == token_file):
                        file_token = cached_file[2]
                        expires_at = cached_file[3]
                    else:
                        with open(token_file) as stream:
                            snapshot = json.load(stream)
                        file_token = snapshot.get('accessToken')
                        expires_at = snapshot.get('expiresAt')
                        _ANTHROPIC_FILE_TOKEN_CACHE = (mtime, token_file, file_token, expires_at or 0.0)
                if not isinstance(file_token, str) or not file_token or (not isinstance(expires_at, (int, float))):
                    raise ValueError('Claude OAuth token snapshot is invalid')
                if expires_at <= time.time() * 1000:
                    raise ValueError('Claude OAuth token snapshot expired; refresh Claude Code login')
            except (json.JSONDecodeError, OSError) as exc:
                raise ValueError('Claude OAuth token snapshot is unavailable') from exc
        elif not os.getenv('CLAUDE_CODE_OAUTH_TOKEN'):
            raise ValueError('Claude OAuth token snapshot is unavailable')
    config = get_config()
    with _anthropic_token_state_lock:
        cached_entry = _OAUTH_TOKEN_CACHE.get(provider_name)
        now = time.time()
        if provider_name == 'anthropic':
            if file_token and (not cached_entry or cached_entry[0] != file_token):
                _anthropic_primary_cooldown_until = 0.0
            if time.monotonic() < _anthropic_primary_cooldown_until:
                fb = os.getenv('ANTHROPIC_FALLBACK_TOKEN')
                if fb:
                    return fb
            env_token = file_token or os.getenv('CLAUDE_CODE_OAUTH_TOKEN')
            if env_token:
                _OAUTH_TOKEN_CACHE[provider_name] = (env_token, now)
                return env_token
        if cached_entry and now - cached_entry[1] < _OAUTH_REFRESH_TTL_SECONDS:
            return cached_entry[0]
    source = config.oat_sources.get(provider_name)
    command = _get_source_command(source)
    if command:
        try:
            result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                token = result.stdout.strip()
                if token:
                    with _anthropic_token_state_lock:
                        _OAUTH_TOKEN_CACHE[provider_name] = (token, time.time())
                    return token
        except Exception:
            pass
    if cached_entry:
        return cached_entry[0]
    token = config.get_oauth_token(provider_name)
    if token:
        with _anthropic_token_state_lock:
            _OAUTH_TOKEN_CACHE[provider_name] = (token, time.time())
    return token

def _fallback_depth_from_metadata(metadata: Any) -> int:
    """Router fallback hops carry fallback_depth > 0; direct client calls do not.

	Accepts either the flat metadata dict or one nesting litellm_metadata;
	missing, boolean, negative, or non-integer values count as a direct call.
	"""
    if not isinstance(metadata, dict):
        return 0
    sources = (metadata, metadata.get('litellm_metadata'))
    for source in sources:
        if not isinstance(source, dict):
            continue
        raw = source.get('fallback_depth')
        if isinstance(raw, int) and (not isinstance(raw, bool)) and (raw > 0):
            return raw
    return 0

def _anthropic_oauth_token_for_model(model_name: Any, model_group: Any=None, fallback_depth: int=0) -> str | None:
    """Pick primary vs fallback OAuth token for the model group being called.

	Fallback-claude-* deployments use ANTHROPIC_FALLBACK_TOKEN only when the
	call is a router-internal fallback hop (fallback_depth > 0): model names
	and groups arrive from client requests, so depth-0 calls must never steer
	credential selection. With no fallback token configured, hops degrade to
	live-token selection instead of dead headers. Everything else keeps
	_get_live_oauth_token semantics (incl. the 429 cooldown fast-path).
	"""
    if fallback_depth > 0 and (isinstance(model_name, str) and model_name in _CLAUDE_FALLBACK_MODEL_GROUPS or (isinstance(model_group, str) and model_group in _CLAUDE_FALLBACK_MODEL_GROUPS)):
        fallback_token = os.getenv('ANTHROPIC_FALLBACK_TOKEN')
        if fallback_token:
            return fallback_token
    try:
        return _get_live_oauth_token('anthropic')
    except ValueError as exc:
        fallback_token = os.getenv('ANTHROPIC_FALLBACK_TOKEN')
        if fallback_token:
            _TOOL_VALIDATION_LOGGER.warning('Primary Claude OAuth token unavailable (%s); failing over to ANTHROPIC_FALLBACK_TOKEN', exc)
            return fallback_token
        raise

_RATE_LIMIT_MESSAGE_MARKERS = ('rate limit', 'rate_limit', 'ratelimit', 'too many requests', 'quota', 'overloaded')

def _is_rate_limit_response(status: Any, message: str) -> bool:
    if status == 429:
        return True
    message_lower = (message or '').lower()
    return any((marker in message_lower for marker in _RATE_LIMIT_MESSAGE_MARKERS))

def _maybe_arm_anthropic_cooldown(kwargs: dict, response_obj: Any) -> None:
    global _anthropic_primary_cooldown_until
    from ccshims.repair import _failure_message
    if _ANTHROPIC_PRIMARY_COOLDOWN_SECONDS <= 0:
        return
    if not os.getenv('ANTHROPIC_FALLBACK_TOKEN'):
        return
    model = str(kwargs.get('model') or '')
    if 'claude' not in model and 'anthropic' not in model:
        return
    metadata = kwargs.get('metadata')
    model_group = str(metadata.get('model_group') or '') if isinstance(metadata, dict) else ''
    if model_group in _CLAUDE_FALLBACK_MODEL_GROUPS:
        return
    fallback_token = os.getenv('ANTHROPIC_FALLBACK_TOKEN')
    if fallback_token:
        used_token = kwargs.get('api_key')
        if not used_token:
            headers = kwargs.get('headers') or kwargs.get('extra_headers') or {}
            if isinstance(headers, dict):
                auth = headers.get('Authorization') or headers.get('authorization') or ''
                used_token = auth.removeprefix('Bearer ').strip()
        if used_token and used_token == fallback_token:
            with _anthropic_token_state_lock:
                probe_at = time.monotonic() + _ANTHROPIC_PRIMARY_PROBE_SECONDS
                if _anthropic_primary_cooldown_until > probe_at:
                    _anthropic_primary_cooldown_until = probe_at
            return
    with _anthropic_token_state_lock:
        if time.monotonic() < _anthropic_primary_cooldown_until:
            return
    status = getattr(response_obj, 'status_code', None)
    msg = _failure_message(response_obj)
    if not _is_rate_limit_response(status, msg):
        return
    with _anthropic_token_state_lock:
        if time.monotonic() < _anthropic_primary_cooldown_until:
            return
        _anthropic_primary_cooldown_until = time.monotonic() + _ANTHROPIC_PRIMARY_COOLDOWN_SECONDS
    _TOOL_VALIDATION_LOGGER.error('anthropic primary 429 -> failover to ANTHROPIC_FALLBACK_TOKEN for %ss (model=%s)', _ANTHROPIC_PRIMARY_COOLDOWN_SECONDS, model)
