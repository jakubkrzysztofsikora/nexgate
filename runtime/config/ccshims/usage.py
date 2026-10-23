# ruff: noqa
from typing import Any
from ccshims.const import _TOOL_VALIDATION_LOGGER


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
    model = kwargs.get('model', 'unknown')
    usage = getattr(response_obj, 'usage', None)
    if usage is None and isinstance(response_obj, dict):
        usage = response_obj.get('usage')
    if usage is None:
        return
    details = None
    if isinstance(usage, dict):
        details = usage.get('prompt_tokens_details')
    else:
        details = getattr(usage, 'prompt_tokens_details', None)
    cache_read = _usage_value(usage, 'cache_read_input_tokens', 'prompt_cache_hit_tokens') or _usage_value(details, 'cached_tokens')
    cache_write = _usage_value(usage, 'cache_creation_input_tokens', 'prompt_cache_miss_tokens')
    prompt_tokens = _usage_value(usage, 'prompt_tokens', 'input_tokens')
    completion_tokens = _usage_value(usage, 'completion_tokens', 'output_tokens')
    billable = prompt_tokens or cache_read + cache_write
    hit_rate = cache_read / billable * 100.0 if billable else 0.0
    model = kwargs.get('model', 'unknown')
    _TOOL_VALIDATION_LOGGER.info('ccproxy cache usage: model=%s prompt=%d completion=%d cache_read=%d cache_write=%d hit_rate=%.1f%%', model, prompt_tokens, completion_tokens, cache_read, cache_write, hit_rate)
    try:
        import json as _json
        import os as _os
        from datetime import datetime as _dt
        path = _os.environ.get('CLAUDE_COMPRESSION_SAVINGS_LOG', '/tmp/token_savings.jsonl')
        _os.makedirs(_os.path.dirname(path) or '.', exist_ok=True)
        entry = {'kind': 'cache', 'timestamp': _dt.utcnow().isoformat() + 'Z', 'model': model, 'cache_read': cache_read, 'cache_write': cache_write, 'prompt_tokens': prompt_tokens, 'completion_tokens': completion_tokens}
        with open(path, 'a', encoding='utf-8') as fh:
            fh.write(_json.dumps(entry) + '\n')
    except Exception:
        pass
