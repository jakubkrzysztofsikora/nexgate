# ruff: noqa
from typing import Any
import os
from ccshims.repair import _is_chatgpt_native_responses_request, _request_is_responses_shape, _system_message_text
from ccshims.const import _TOOL_VALIDATION_LOGGER


_UTILITY_PROMPT_MARKERS = ('generate a concise title', 'write a 5-10 word title', 'generate a title for this conversation', 'summarize this conversation', 'analyze if this message indicates a new conversation topic', 'extract any file paths that this command reads or modifies')

_UTILITY_SMALL_REQUEST_CHARS = 2000

_TERSE_DIRECTIVE = '\n\nOutput budget: answer in the fewest words that fully resolve the request. No preamble, no recap, no narration of your own process, no summary of code you just wrote. Prefer the smallest correct diff and reuse what already exists over writing new code.'

def _cheap_utility_model() -> str:
    return os.environ.get('CCPROXY_CHEAP_MODEL', '').strip()

def _utility_max_tokens() -> int:
    try:
        return max(1, int(os.environ.get('CCPROXY_UTILITY_MAX_TOKENS', '512')))
    except ValueError:
        return 512

def _terse_mode_enabled() -> bool:
    return os.environ.get('CCPROXY_TERSE_MODE', '').lower() in {'1', 'true', 'yes', 'on'}

def _tool_description_limit() -> int:
    try:
        return max(0, int(os.environ.get('CCPROXY_TOOL_DESC_MAX_CHARS', '0')))
    except ValueError:
        return 0

def _request_text(data: dict[str, Any]) -> str:
    """Flatten system + messages into one lowercase string for marker matching."""
    parts: list[str] = []
    system = data.get('system')
    if isinstance(system, str):
        parts.append(system)
    elif isinstance(system, list):
        parts.append(_system_message_text(system))
    messages = data.get('messages')
    if isinstance(messages, list):
        for message in messages:
            if isinstance(message, dict):
                parts.append(_system_message_text(message.get('content')))
    return '\n'.join(parts).lower()

def _is_utility_request(data: dict[str, Any]) -> bool:
    """True for Claude Code background calls that do not need a frontier model.

	Deliberately conservative: a false positive downgrades a real coding turn to
	a cheap model, which is far worse than missing a saving. Requires either an
	explicit Claude Code utility marker, or a tool-free Haiku-tier request small
	enough that it cannot be a real agentic turn.
	"""
    if data.get('tools'):
        return False
    text = _request_text(data)
    if any((marker in text for marker in _UTILITY_PROMPT_MARKERS)):
        return True
    model = str(data.get('model') or '').lower()
    return 'haiku' in model and len(text) < _UTILITY_SMALL_REQUEST_CHARS

def _route_utility_request(data: dict[str, Any]) -> bool:
    """Send Claude Code utility calls to a cheap model. Returns True if rerouted.

	Only utility calls are rerouted. The main session is never downgraded
	mid-conversation: each model has its own prompt cache, so switching models
	100k tokens in costs more than letting the expensive model answer.
	"""
    cheap_model = _cheap_utility_model()
    if not cheap_model or not _is_utility_request(data):
        return False
    original = data.get('model')
    if original == cheap_model:
        return False
    data['model'] = cheap_model
    data['max_tokens'] = min(_utility_max_tokens(), data.get('max_tokens') or _utility_max_tokens())
    data.pop('max_completion_tokens', None)
    data['stream'] = False
    data.pop('thinking', None)
    data['reasoning_effort'] = 'low'
    metadata = data.get('metadata')
    if not isinstance(metadata, dict):
        metadata = {}
        data['metadata'] = metadata
    metadata['ccproxy_utility_route'] = True
    _TOOL_VALIDATION_LOGGER.info('ccproxy utility reroute: %s -> %s max_tokens=%s', original, cheap_model, data['max_tokens'])
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
    system = data.get('system')
    if isinstance(system, str):
        if _TERSE_DIRECTIVE not in system:
            data['system'] = system + _TERSE_DIRECTIVE
        return
    if isinstance(system, list):
        for block in reversed(system):
            if isinstance(block, dict) and block.get('type') == 'text':
                text = block.get('text')
                if isinstance(text, str) and _TERSE_DIRECTIVE not in text:
                    block['text'] = text + _TERSE_DIRECTIVE
                return
    if _is_chatgpt_native_responses_request(data):
        return
    if _request_is_responses_shape(data):
        instructions = data.get('instructions')
        if isinstance(instructions, str):
            if _TERSE_DIRECTIVE not in instructions:
                data['instructions'] = instructions + _TERSE_DIRECTIVE
        elif instructions is None:
            data['instructions'] = _TERSE_DIRECTIVE
        return
    messages = data.get('messages')
    if not isinstance(messages, list):
        return
    for message in messages:
        if not isinstance(message, dict) or message.get('role') not in {'system', 'developer'}:
            continue
        content = message.get('content')
        if isinstance(content, str):
            if _TERSE_DIRECTIVE not in content:
                message['content'] = content + _TERSE_DIRECTIVE
            return
        if isinstance(content, list):
            for block in reversed(content):
                if isinstance(block, dict) and block.get('type') == 'text':
                    text = block.get('text')
                    if isinstance(text, str) and _TERSE_DIRECTIVE not in text:
                        block['text'] = text + _TERSE_DIRECTIVE
                    return
            return

def _truncate_description(value: Any, limit: int) -> Any:
    if not isinstance(value, str) or len(value) <= limit:
        return value
    return value[:limit].rstrip() + '…'

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
        if key == 'examples':
            continue
        if key == 'description':
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
    tools = data.get('tools')
    if not isinstance(tools, list):
        return
    minified: list[Any] = []
    for tool in tools:
        if not isinstance(tool, dict):
            minified.append(tool)
            continue
        entry = dict(tool)
        if 'description' in entry:
            entry['description'] = _truncate_description(entry['description'], limit)
        for schema_key in ('input_schema', 'parameters'):
            if isinstance(entry.get(schema_key), dict):
                entry[schema_key] = _minify_tool_schema(entry[schema_key], limit)
        function = entry.get('function')
        if isinstance(function, dict):
            function = dict(function)
            if 'description' in function:
                function['description'] = _truncate_description(function['description'], limit)
            if isinstance(function.get('parameters'), dict):
                function['parameters'] = _minify_tool_schema(function['parameters'], limit)
            entry['function'] = function
        minified.append(entry)
    data['tools'] = minified
