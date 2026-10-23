# ruff: noqa
from typing import Any
import os
from ccshims import get_llm_provider
from ccshims.const import _TOOL_VALIDATION_LOGGER
from ccshims.oauth import _anthropic_oauth_token_for_model, _fallback_depth_from_metadata, _normalize_claude_metadata_model_group
from ccshims.repair import _coerce_anthropic_provider_for_sanitization, _required_fields_by_tool, _sanitize_history_tool_calls, _sanitize_model_call_payload
from ccshims.tele import _load_model_name_to_provider_map, _strip_null_bytes


def _drop_header(headers: dict[str, Any], name: str) -> None:
    for key in list(headers):
        if isinstance(key, str) and key.lower() == name.lower():
            headers.pop(key, None)

_CLIENT_AUTH_HEADER_NAMES = ('authorization', 'x-api-key', 'x-litellm-api-key', 'anthropic-version', 'anthropic-beta')

def _iter_provider_specific_entries(data: dict[str, Any]) -> list[dict[str, Any]]:
    """LiteLLM 1.100 sends provider_specific_header as one dict or a list of scoped dicts."""
    value = data.get('provider_specific_header')
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [entry for entry in value if isinstance(entry, dict)]
    return []

def _scoped_entry_extra_headers(entry: dict[str, Any]) -> dict[str, Any]:
    extra_headers = entry.get('extra_headers')
    if not isinstance(extra_headers, dict):
        extra_headers = {}
        entry['extra_headers'] = extra_headers
    return extra_headers

def _anthropic_scoped_extra_headers(data: dict[str, Any], *, exact_only: bool=False, create: bool=False) -> list[dict[str, Any]]:
    """Extra-header dicts for provider_specific_header entries scoped to anthropic.

	exact_only narrows to entries whose scope is exactly "anthropic", so the
	OAuth bearer never rides on entries shared with bedrock/vertex_ai.
	"""
    matched: list[dict[str, Any]] = []
    for entry in _iter_provider_specific_entries(data):
        providers = [p.strip() for p in str(entry.get('custom_llm_provider') or '').split(',')]
        if 'anthropic' not in providers:
            continue
        if exact_only and providers != ['anthropic']:
            continue
        matched.append(_scoped_entry_extra_headers(entry))
    if matched or not create:
        return matched
    entry = {'custom_llm_provider': 'anthropic', 'extra_headers': {}}
    value = data.get('provider_specific_header')
    if isinstance(value, list):
        value.append(entry)
    elif isinstance(value, dict):
        data['provider_specific_header'] = [value, entry]
    else:
        data['provider_specific_header'] = entry
    return [entry['extra_headers']]

def _strip_client_auth_headers(data: dict[str, Any]) -> None:
    """Remove client/proxy auth headers before non-Anthropic provider calls."""
    for header_key in ('headers', 'extra_headers'):
        headers = data.get(header_key)
        if isinstance(headers, dict):
            for header_name in _CLIENT_AUTH_HEADER_NAMES:
                _drop_header(headers, header_name)
    for entry in _iter_provider_specific_entries(data):
        extra_headers = entry.get('extra_headers')
        if isinstance(extra_headers, dict):
            for header_name in _CLIENT_AUTH_HEADER_NAMES:
                _drop_header(extra_headers, header_name)
    proxy_request = data.get('proxy_server_request')
    if isinstance(proxy_request, dict):
        headers = proxy_request.get('headers')
        if isinstance(headers, dict):
            for header_name in _CLIENT_AUTH_HEADER_NAMES:
                _drop_header(headers, header_name)

_CLAUDE_OAUTH_BILLING_HEADER = 'x-anthropic-billing-header: cc_version=2.1.294.725; cc_entrypoint=sdk-cli;'

def _text_has_billing_header(value: Any) -> bool:
    return isinstance(value, str) and 'x-anthropic-billing-header:' in value

def _payload_has_billing_header(data: dict[str, Any]) -> bool:
    system = data.get('system')
    if _text_has_billing_header(system):
        return True
    if isinstance(system, list):
        for block in system:
            if isinstance(block, dict) and _text_has_billing_header(block.get('text')):
                return True
    messages = data.get('messages')
    if isinstance(messages, list):
        for message in messages:
            if not isinstance(message, dict) or message.get('role') != 'system':
                continue
            content = message.get('content')
            if _text_has_billing_header(content):
                return True
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and _text_has_billing_header(block.get('text')):
                        return True
    return _text_has_billing_header(data.get('instructions'))

def _inject_claude_oauth_billing_header(data: dict[str, Any]) -> None:
    """Put the Claude Code billing line on a server-held setup-token call.

	Chat completions pick it up from a leading system message. Anthropic
	Messages calls pick it up from the top-level system field. Already-present
	client lines are left alone so the prefix stays cache-stable.
	"""
    if _payload_has_billing_header(data):
        return
    system = data.get('system')
    proxy_request = data.get('proxy_server_request')
    url = proxy_request.get('url') if isinstance(proxy_request, dict) else ''
    anthropic_messages = '/v1/messages' in str(url) or (isinstance(system, list) and any((isinstance(block, dict) and block.get('type') for block in system)))
    if anthropic_messages or 'system' in data:
        block = {'type': 'text', 'text': _CLAUDE_OAUTH_BILLING_HEADER}
        if isinstance(system, list):
            data['system'] = [block, *system]
        elif isinstance(system, str) and system:
            data['system'] = f'{_CLAUDE_OAUTH_BILLING_HEADER}\n{system}'
        else:
            data['system'] = _CLAUDE_OAUTH_BILLING_HEADER
        return
    messages = data.get('messages')
    if isinstance(messages, list):
        if messages and isinstance(messages[0], dict) and (messages[0].get('role') == 'system'):
            content = messages[0].get('content')
            if isinstance(content, str):
                messages[0]['content'] = f'{_CLAUDE_OAUTH_BILLING_HEADER}\n{content}'
                return
            if isinstance(content, list):
                messages[0]['content'] = [{'type': 'text', 'text': _CLAUDE_OAUTH_BILLING_HEADER}, *content]
                return
        data['messages'] = [{'role': 'system', 'content': _CLAUDE_OAUTH_BILLING_HEADER}, *messages]
        return
    data['system'] = _CLAUDE_OAUTH_BILLING_HEADER

def _set_anthropic_oauth_headers(data: dict[str, Any], auth_header: str) -> None:
    """Force Anthropic calls to use the live OAuth bearer, not client auth."""
    data['api_key'] = auth_header.removeprefix('Bearer ')
    _inject_claude_oauth_billing_header(data)
    for header_key in ('headers', 'extra_headers'):
        headers = data.get(header_key)
        if not isinstance(headers, dict):
            headers = {}
            data[header_key] = headers
        _drop_header(headers, 'authorization')
        _drop_header(headers, 'x-api-key')
        headers['authorization'] = auth_header
    for extra_headers in _anthropic_scoped_extra_headers(data):
        _drop_header(extra_headers, 'authorization')
        _drop_header(extra_headers, 'x-api-key')
    for extra_headers in _anthropic_scoped_extra_headers(data, exact_only=True, create=True):
        extra_headers['authorization'] = auth_header
    proxy_request = data.get('proxy_server_request')
    if isinstance(proxy_request, dict) and isinstance(proxy_request.get('headers'), dict):
        _drop_header(proxy_request['headers'], 'authorization')
        _drop_header(proxy_request['headers'], 'x-api-key')

def forward_provider_oauth(data: dict[str, Any], user_api_key_dict: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    """Inject provider oauth headers in the shape LiteLLM's pass-through expects.

	Upstream ccproxy's forward_oauth hook sets only extra_headers. LiteLLM's
	Anthropic /v1/messages path drops those headers unless custom_llm_provider
	is also set on provider_specific_header.

	This callback handles both ccproxy-routed requests AND direct model requests
	for Claude (Anthropic) models that use OAuth instead of API keys.
	"""
    from ccproxy.config import get_config
    metadata = data.get('metadata', {})
    if not isinstance(metadata, dict):
        metadata = {}
    model_config = metadata.get('ccproxy_model_config') or {}
    if not isinstance(model_config, dict):
        model_config = {}
    litellm_params = model_config.get('litellm_params', {})
    if not isinstance(litellm_params, dict):
        litellm_params = {}
    if isinstance(data.get('messages'), list):
        data['messages'] = _strip_null_bytes(data['messages'])
    if 'system' in data:
        data['system'] = _strip_null_bytes(data['system'])
    if 'tools' in data:
        data['tools'] = _strip_null_bytes(data['tools'])
    proxy_request = data.get('proxy_server_request')
    if isinstance(proxy_request, dict):
        if 'body' in proxy_request:
            proxy_request['body'] = _strip_null_bytes(proxy_request['body'])
        if 'headers' in proxy_request:
            proxy_request['headers'] = _strip_null_bytes(proxy_request['headers'])
    if _normalize_claude_metadata_model_group(data):
        metadata = data.get('metadata', {})
        if not isinstance(metadata, dict):
            metadata = {}
    routed_model = data.get('model') or metadata.get('ccproxy_litellm_model') or ''
    if not routed_model:
        return data
    provider_name = None
    alias_map = _load_model_name_to_provider_map()
    provider_name = alias_map.get(routed_model)
    if not provider_name:
        if routed_model.startswith('claude-') or routed_model.startswith('fallback-claude-'):
            provider_name = 'anthropic'
        elif metadata.get('ccproxy_model_config'):
            try:
                _, provider_name, _, _ = get_llm_provider(model=routed_model, custom_llm_provider=litellm_params.get('custom_llm_provider'), api_base=litellm_params.get('api_base'))
            except Exception:
                pass
        if not provider_name:
            try:
                _, provider_name, _, _ = get_llm_provider(model=routed_model)
            except Exception:
                provider_name = None
    provider_name = _coerce_anthropic_provider_for_sanitization(data, routed_model, provider_name)
    if not provider_name:
        return data
    _sanitize_model_call_payload(data, provider_name)
    if provider_name != 'anthropic':
        try:
            required_map = _required_fields_by_tool(data)
            if required_map:
                sanitized, patched = _sanitize_history_tool_calls(data.get('messages'), required_map)
                if patched:
                    data['messages'] = sanitized
                    _TOOL_VALIDATION_LOGGER.info('history tool-call cleanup: patched=%d model=%s provider=%s', patched, routed_model, provider_name)
        except Exception as exc:
            _TOOL_VALIDATION_LOGGER.debug('history tool-call cleanup raised: %s', exc, exc_info=True)
    if provider_name != 'anthropic':
        for token_field in ('max_completion_tokens', 'max_tokens'):
            if token_field in data:
                value = data[token_field]
                if isinstance(value, (int, float)) and value < 1:
                    data[token_field] = 1024
                    _TOOL_VALIDATION_LOGGER.error('FINAL CLAMP: %s=%s for provider=%s', token_field, value, provider_name)
        return data
    request = data.get('proxy_server_request') or {}
    headers = request.get('headers', {}) if isinstance(request, dict) else {}
    try:
        oauth_token = _anthropic_oauth_token_for_model(routed_model, metadata.get('model_group'), fallback_depth=_fallback_depth_from_metadata(metadata))
    except ValueError as exc:
        _TOOL_VALIDATION_LOGGER.error('Failed to acquire Claude OAuth token: %s', exc)
        oauth_token = os.getenv('ANTHROPIC_FALLBACK_TOKEN')
    if not oauth_token:
        return data
    auth_header = oauth_token if oauth_token.startswith('Bearer ') else f'Bearer {oauth_token}'
    config = get_config()
    _set_anthropic_oauth_headers(data, auth_header)
    extra_headers_list = _anthropic_scoped_extra_headers(data, exact_only=True, create=True)
    custom_user_agent = config.get_oauth_user_agent(provider_name)
    if custom_user_agent:
        for extra_headers in extra_headers_list:
            extra_headers['user-agent'] = custom_user_agent
    elif headers.get('user-agent'):
        for extra_headers in extra_headers_list:
            extra_headers.setdefault('user-agent', headers['user-agent'])
    return data
