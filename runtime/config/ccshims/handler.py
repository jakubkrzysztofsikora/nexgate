# ruff: noqa
from typing import Any
import asyncio
import uuid
from ccshims import CCProxyHandler
from ccshims.repair import _AnthropicSSEStreamSanitizer, _apply_tool_search_defer, _convert_native_text_tool_calls, _convert_planned_agent_tool, _convert_planned_read_tool, _convert_single_tool_text_command, _duration_ms, _failure_message, _is_anthropic_messages_stream, _is_chatgpt_native_responses_request, _log_tool_search_discovery, _provider_for_model_call, _repair_planned_agent_final_text, _request_is_responses_shape, _required_fields_by_tool, _response_repair_should_bypass, _rewrite_planned_tool_call, _sanitize_anthropic_sse_stream_items, _sanitize_model_call_payload, _sanitize_response_text_artifacts, _sanitize_response_tool_calls, _should_buffer_tool_stream, _streamed_command_tool_events, _streamed_native_tool_events, _streamed_planned_agent_final_events, _streamed_planned_tool_rewrite_events, _strip_tool_search_artifacts
from ccshims.const import _CLAUDE_PRIMARY_PROVIDER_MODEL_VALUES, _TOOL_VALIDATION_LOGGER
from ccshims.oauth import _anthropic_oauth_token_for_model, _anthropic_ping_event, _fallback_depth_from_metadata, _force_claude_primary_passthrough, _maybe_arm_anthropic_cooldown, _normalize_claude_metadata_model_group, _stream_buffer_timeout_seconds
from ccshims.tele import _emit_savings_event, _sticky_experiment_cohort
from ccshims.utility import _inject_terse_directive, _minify_tool_payload, _route_utility_request, _terse_mode_enabled
from ccshims.usage import _log_cache_usage, _usage_value
from ccshims.hdr import _set_anthropic_oauth_headers, _strip_client_auth_headers, forward_provider_oauth


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
            user_api_key_dict = args[0]
            data = args[2]
            official_proxy_signature = True
        elif args and isinstance(args[0], dict):
            data = args[0]
            user_api_key_dict = args[1] if len(args) > 1 else {}
        elif isinstance(kwargs.get('data'), dict):
            data = kwargs['data']
            user_api_key_dict = kwargs.get('user_api_key_dict', {})
            official_proxy_signature = True
        if data is None:
            parent_hook = getattr(CCProxyHandler, 'async_pre_call_hook', None)
            if parent_hook is None:
                return None
            return await parent_hook(self, *args, **kwargs)
        requested_model = data.get('model')
        metadata = data.get('metadata')
        if not isinstance(metadata, dict):
            metadata = {}
            data['metadata'] = metadata
        metadata.setdefault('ccproxy_requested_model', requested_model)
        metadata.setdefault('ccproxy_root_request_id', uuid.uuid4().hex)
        metadata['ccproxy_tool_search_cohort'] = _sticky_experiment_cohort(data, 'TOOL_SEARCH')
        if _request_is_responses_shape(data):
            metadata = data.get('metadata')
            if not isinstance(metadata, dict):
                metadata = {}
                data['metadata'] = metadata
            metadata['ccproxy_responses_shape'] = True
        _minify_tool_payload(data)
        _inject_terse_directive(data)
        if _terse_mode_enabled() and (not _is_chatgpt_native_responses_request(data)):
            metadata['ccproxy_terse_applied'] = True
        if _route_utility_request(data):
            requested_model = data['model']
        _normalize_claude_metadata_model_group(data)
        _force_claude_primary_passthrough(data, requested_model)
        _emit_savings_event(data, 'accepted')
        if official_proxy_signature:
            forward_kwargs = dict(kwargs)
            for key in ('data', 'user_api_key_dict', 'cache', 'call_type'):
                forward_kwargs.pop(key, None)
            data = forward_provider_oauth(data, user_api_key_dict or {}, **forward_kwargs)
            return data
        parent_hook = getattr(CCProxyHandler, 'async_pre_call_hook', None)
        if parent_hook is not None:
            parent_kwargs = dict(kwargs)
            for key in ('data', 'user_api_key_dict', 'cache', 'call_type'):
                parent_kwargs.pop(key, None)
            data = await parent_hook(self, data, user_api_key_dict, **parent_kwargs)
        if _force_claude_primary_passthrough(data, requested_model):
            _TOOL_VALIDATION_LOGGER.info('claude primary passthrough restored after ccproxy routing: model=%s', requested_model)
        _apply_tool_search_defer(data)
        return data

    async def async_pre_call_deployment_hook(self, kwargs: dict[str, Any], call_type: Any) -> dict[str, Any] | None:
        """Scrub the final provider payload after LiteLLM picks a deployment.

		LiteLLM can convert Anthropic `/v1/messages` requests into chat payloads
		after `async_pre_call_hook` has already run. When fallback then selects a
		non-Anthropic provider, Claude `thinking_blocks` can leak into Mistral or
		OpenAI-compatible APIs. This deployment hook runs late enough to see the
		actual provider payload for every fallback attempt.
		"""
        for token_field in ('max_completion_tokens', 'max_tokens'):
            if token_field in kwargs:
                value = kwargs[token_field]
                if isinstance(value, (int, float)) and value < 1:
                    kwargs[token_field] = 1024
                    _TOOL_VALIDATION_LOGGER.error('ULTIMATE CLAMP: %s=%s for kwargs', token_field, value)
        provider_name = _provider_for_model_call(kwargs)
        if not provider_name:
            return kwargs
        metadata = kwargs.get('metadata')
        if not isinstance(metadata, dict):
            metadata = {}
        if provider_name != 'anthropic' and metadata.get('ccproxy_provider_model') in _CLAUDE_PRIMARY_PROVIDER_MODEL_VALUES:
            kwargs.pop('api_key', None)
            kwargs.pop('api_base', None)
        if provider_name != 'anthropic':
            _strip_client_auth_headers(kwargs)
            _strip_tool_search_artifacts(kwargs)
        cleaned = _sanitize_model_call_payload(kwargs, provider_name)
        if provider_name == 'anthropic':
            oauth_token = _anthropic_oauth_token_for_model(kwargs.get('model'), metadata.get('model_group'), fallback_depth=_fallback_depth_from_metadata(kwargs) or _fallback_depth_from_metadata(metadata))
            if oauth_token:
                auth_header = oauth_token if oauth_token.startswith('Bearer ') else f'Bearer {oauth_token}'
                _set_anthropic_oauth_headers(cleaned, auth_header)
        return cleaned

    async def async_log_success_event(self, kwargs: dict[str, Any], response_obj: Any, start_time: Any, end_time: Any) -> None:
        """Log prompt-cache usage. Fires for streaming and non-streaming alike."""
        try:
            _log_cache_usage(kwargs, response_obj)
            usage = getattr(response_obj, 'usage', None)
            if usage is None and isinstance(response_obj, dict):
                usage = response_obj.get('usage')
            _emit_savings_event(kwargs, 'success', input_tokens=_usage_value(usage, 'prompt_tokens', 'input_tokens'), output_tokens=_usage_value(usage, 'completion_tokens', 'output_tokens'))
        except Exception:
            _TOOL_VALIDATION_LOGGER.debug('cache usage logging failed', exc_info=True)

    async def async_log_failure_event(self, kwargs: dict[str, Any], response_obj: Any, start_time: Any, end_time: Any) -> None:
        failure_obj = response_obj if response_obj is not None else kwargs.get('exception')
        if failure_obj is None:
            _emit_savings_event(kwargs, 'failed_unknown')
            _TOOL_VALIDATION_LOGGER.debug('ccproxy ignored empty failure callback: model=%s duration_ms=%s', kwargs.get('model', 'unknown'), _duration_ms(start_time, end_time) or 'unknown')
            return
        metadata = kwargs.get('metadata')
        if not isinstance(metadata, dict):
            metadata = {}
        duration = _duration_ms(start_time, end_time)
        _TOOL_VALIDATION_LOGGER.error('ccproxy request failed: model_name=%s model=%s error_type=%s duration_ms=%s error=%s', metadata.get('ccproxy_model_name', 'unknown'), kwargs.get('model', 'unknown'), type(failure_obj).__name__, duration if duration is not None else 'unknown', _failure_message(failure_obj) or 'unknown')
        _emit_savings_event(kwargs, 'provider_error', duration_ms=duration, error_type=type(failure_obj).__name__)
        try:
            _maybe_arm_anthropic_cooldown(kwargs, failure_obj)
        except Exception:
            _TOOL_VALIDATION_LOGGER.debug('cooldown arm failed', exc_info=True)

    async def async_post_call_success_hook(self, data: dict[str, Any], user_api_key_dict: Any, response: Any) -> Any:
        if _response_repair_should_bypass(data):
            _TOOL_VALIDATION_LOGGER.debug('Responses-shape reply bypasses Claude textual tool recovery: model=%s', data.get('model'))
            parent_hook = getattr(CCProxyHandler, 'async_post_call_success_hook', None)
            if parent_hook is not None and parent_hook is not type(self).async_post_call_success_hook:
                return await parent_hook(self, data, user_api_key_dict, response)
            return response
        try:
            native_tool_patched = _convert_native_text_tool_calls(response, data)
            if native_tool_patched:
                _TOOL_VALIDATION_LOGGER.info('response native tool-call conversion: patched=%d model=%s', native_tool_patched, data.get('model'))
            planned_read_patched = _convert_planned_read_tool(response, data)
            if planned_read_patched:
                _TOOL_VALIDATION_LOGGER.info('response planned-read tool conversion: patched=%d model=%s', planned_read_patched, data.get('model'))
            planned_agent_patched = _convert_planned_agent_tool(response, data)
            if planned_agent_patched:
                _TOOL_VALIDATION_LOGGER.info('response planned-agent tool conversion: patched=%d model=%s', planned_agent_patched, data.get('model'))
            planned_agent_final_patched = _repair_planned_agent_final_text(response, data)
            if planned_agent_final_patched:
                _TOOL_VALIDATION_LOGGER.info('response planned-agent final repair: patched=%d model=%s', planned_agent_final_patched, data.get('model'))
            command_tool_patched = _convert_single_tool_text_command(response, data)
            if command_tool_patched:
                _TOOL_VALIDATION_LOGGER.info('response bare-command tool conversion: patched=%d model=%s', command_tool_patched, data.get('model'))
            planned_tool_patched = _rewrite_planned_tool_call(response, data)
            if planned_tool_patched:
                _TOOL_VALIDATION_LOGGER.info('response planned-tool rewrite: patched=%d model=%s', planned_tool_patched, data.get('model'))
            text_patched = _sanitize_response_text_artifacts(response)
            if text_patched:
                _TOOL_VALIDATION_LOGGER.info('response think-tag cleanup: patched=%d model=%s', text_patched, data.get('model'))
            required_map = _required_fields_by_tool(data)
            if required_map:
                patched = _sanitize_response_tool_calls(response, required_map)
                if patched:
                    _TOOL_VALIDATION_LOGGER.info('response tool-call validation: patched=%d model=%s', patched, data.get('model'))
        except Exception as exc:
            _TOOL_VALIDATION_LOGGER.debug('response tool-call validation raised: %s', exc, exc_info=True)
        parent_hook = getattr(CCProxyHandler, 'async_post_call_success_hook', None)
        if parent_hook is not None and parent_hook is not type(self).async_post_call_success_hook:
            return await parent_hook(self, data, user_api_key_dict, response)
        return response

    async def async_post_call_streaming_iterator_hook(self, user_api_key_dict: Any, response: Any, request_data: dict[str, Any]):
        if _response_repair_should_bypass(request_data):
            _TOOL_VALIDATION_LOGGER.debug('Responses-shape stream bypasses legacy buffering/recovery: model=%s', request_data.get('model'))
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
                _TOOL_VALIDATION_LOGGER.info('stream thinking-delta validation: dropped=%d model=%s', sanitizer.dropped_thinking_deltas, request_data.get('model'))
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
                        done, _ = await asyncio.wait({pending_item}, timeout=buffer_timeout, return_when=asyncio.FIRST_COMPLETED)
                        if done:
                            done_item = pending_item
                            pending_item = None
                            item = await done_item
                            break
                        try:
                            tool_events = _streamed_planned_tool_rewrite_events(buffered, request_data)
                            if tool_events is None:
                                tool_events = _streamed_native_tool_events([], request_data)
                            if tool_events is None:
                                tool_events = _streamed_command_tool_events([], request_data)
                        except Exception as exc:
                            _TOOL_VALIDATION_LOGGER.debug('stream timeout tool conversion raised: %s', exc, exc_info=True)
                            tool_events = None
                        if tool_events:
                            _TOOL_VALIDATION_LOGGER.info('stream timeout planned-tool conversion: patched=%d model=%s timeout=%s buffered=%d', 1, request_data.get('model'), buffer_timeout, len(buffered))
                            pending_item.cancel()
                            pending_item = None
                            for event in tool_events:
                                yield event
                            return
                        if buffered:
                            _TOOL_VALIDATION_LOGGER.debug('stream buffer timeout without planned tool; sending keepalive: model=%s timeout=%s buffered=%d', request_data.get('model'), buffer_timeout, len(buffered))
                        else:
                            _TOOL_VALIDATION_LOGGER.debug('stream first-chunk timeout without planned tool; sending keepalive: model=%s timeout=%s', request_data.get('model'), buffer_timeout)
                        yield _anthropic_ping_event()
            except StopAsyncIteration:
                break
            finally:
                if pending_item is not None and (not pending_item.done()):
                    pending_item.cancel()
            buffered.append(item)
        try:
            planned_stream_patched = False
            tool_events = _streamed_planned_tool_rewrite_events(buffered, request_data)
            if tool_events:
                planned_stream_patched = True
                _TOOL_VALIDATION_LOGGER.info('stream planned-tool rewrite: patched=%d model=%s', 1, request_data.get('model'))
            if tool_events is None:
                tool_events = _streamed_native_tool_events(buffered, request_data)
            if tool_events and (not planned_stream_patched):
                _TOOL_VALIDATION_LOGGER.info('stream native tool-call conversion: patched=%d model=%s', 1, request_data.get('model'))
            if tool_events is None:
                tool_events = _streamed_command_tool_events(buffered, request_data)
        except Exception as exc:
            _TOOL_VALIDATION_LOGGER.debug('stream command-tool conversion raised: %s', exc, exc_info=True)
            tool_events = None
        if tool_events:
            _TOOL_VALIDATION_LOGGER.info('stream command-tool conversion: patched=%d model=%s', 1, request_data.get('model'))
            for event in tool_events:
                yield event
            return
        try:
            final_events = _streamed_planned_agent_final_events(buffered, request_data)
        except Exception as exc:
            _TOOL_VALIDATION_LOGGER.debug('stream planned-agent final repair raised: %s', exc, exc_info=True)
            final_events = None
        if final_events:
            _TOOL_VALIDATION_LOGGER.info('stream planned-agent final repair: patched=%d model=%s', 1, request_data.get('model'))
            _log_tool_search_discovery(buffered, request_data)
            for event in final_events:
                yield event
            return
        sanitized_items, dropped = _sanitize_anthropic_sse_stream_items(buffered)
        if dropped:
            _TOOL_VALIDATION_LOGGER.info('stream thinking-delta validation: dropped=%d model=%s', dropped, request_data.get('model'))
        _log_tool_search_discovery(buffered, request_data)
        for item in sanitized_items:
            yield item

ccproxy_handler = _ValidatingCCProxyHandler()
