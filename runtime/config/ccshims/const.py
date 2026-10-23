# ruff: noqa
import logging
import os
import queue


_LITELLM_CONFIG_PATH = os.getenv('LITELLM_PROXY_CONFIG_PATH', '/app/config.yaml')

_MAX_COMPLETION_TOKENS_LIMIT = int(os.environ.get('MAX_COMPLETION_TOKENS_LIMIT', '16384'))

_SAVINGS_EVENT_SCHEMA = 1

_SAVINGS_EVENT_PATH = os.getenv('CCPROXY_SAVINGS_EVENT_LOG', '/host/logs/savings_events.jsonl')

_SAVINGS_EVENT_SALT = os.getenv('CCPROXY_SAVINGS_EVENT_SALT', '')

_SAVINGS_EVENTS_ENABLED = bool(_SAVINGS_EVENT_SALT)

_SAVINGS_EVENT_MAX_BYTES = int(os.getenv('CCPROXY_SAVINGS_EVENT_MAX_BYTES', str(50 * 1024 * 1024)))

_SAVINGS_EVENT_QUEUE: queue.Queue[str] = queue.Queue(maxsize=int(os.getenv('CCPROXY_SAVINGS_EVENT_QUEUE_SIZE', '1000')))

_HARNESS_HEADER = os.getenv('CCPROXY_HARNESS_HEADER', 'x-sovereign-harness').lower()

_HARNESS_TOKEN = os.getenv('CCPROXY_HARNESS_TOKEN', '')

_KNOWN_HARNESSES = frozenset({'claude_code', 'codex_native', 'codex_bridged'})

_ANTHROPIC_EMPTY_TEXT_PLACEHOLDER = '[empty]'

_TOOL_SEARCH_FLAG = os.getenv('CCPROXY_TOOL_SEARCH', '').strip().lower()

_TOOL_SEARCH_ENABLED = _TOOL_SEARCH_FLAG in {'1', 'true', 'yes', 'on'}

_TOOL_SEARCH_MIN = int(os.getenv('CCPROXY_TOOL_SEARCH_MIN', '30'))

_TOOL_SEARCH_BETA = 'advanced-tool-use-2025-11-20'

_TOOL_SEARCH_MODELS = frozenset({'claude-opus-4-8', 'claude-opus-4-8[1m]', 'claude-opus-5', 'claude-opus-5[1m]', 'claude-opus-5-5', 'claude-opus-5-5[1m]', 'claude-sonnet-5', 'claude-sonnet-5[1m]', 'claude-sonnet-5-5', 'claude-sonnet-5-5[1m]', 'claude-haiku-5-5', 'claude-haiku-5-5[1m]', 'claude-fable-5-1'})

_TOOL_SEARCH_HOT_TOOLS = frozenset({'Read', 'Edit', 'Write', 'Bash', 'Grep', 'Glob', 'Task', 'Agent', 'TaskUpdate', 'TodoWrite', 'Search', 'WebSearch', 'Web Search', 'ReadURL', 'Fetch'})

_TOOL_SEARCH_TOOL = {'type': 'tool_search_tool_bm25_20251119', 'name': 'tool_search_tool_bm25'}

_TOOL_VALIDATION_LOGGER = logging.getLogger('ccproxy.tool_validation')

_CLAUDE_PRIMARY_PROVIDER_MODELS = {'claude-chat': 'anthropic/claude-haiku-4-5-20251001', 'claude-haiku-4-5-20251001': 'anthropic/claude-haiku-4-5-20251001', 'claude-haiku-5-5': 'anthropic/claude-haiku-5-5', 'claude-haiku-5-5[1m]': 'anthropic/claude-haiku-5-5', 'haiku-5-5': 'anthropic/claude-haiku-5-5', 'claude-opus-4-8': 'anthropic/claude-opus-4-8', 'claude-opus-4-8[1m]': 'anthropic/claude-opus-4-8', 'opus[1m]': 'anthropic/claude-opus-4-8', 'claude-opus-5': 'anthropic/claude-opus-5', 'claude-opus-5[1m]': 'anthropic/claude-opus-5', 'claude-opus-5-5': 'anthropic/claude-opus-5-5', 'claude-opus-5-5[1m]': 'anthropic/claude-opus-5-5', 'claude-sonnet-4-6': 'anthropic/claude-sonnet-4-6', 'claude-sonnet-5': 'anthropic/claude-sonnet-5', 'claude-sonnet-5[1m]': 'anthropic/claude-sonnet-5', 'claude-sonnet-5-5': 'anthropic/claude-sonnet-5-5', 'claude-sonnet-5-5[1m]': 'anthropic/claude-sonnet-5-5', 'claude-fable-5-1': 'anthropic/claude-fable-5-1'}

_CLAUDE_PRIMARY_MODEL_GROUPS = set(_CLAUDE_PRIMARY_PROVIDER_MODELS)

_CLAUDE_PRIMARY_PROVIDER_MODEL_VALUES = set(_CLAUDE_PRIMARY_PROVIDER_MODELS.values())

_CLAUDE_FALLBACK_PROVIDER_MODELS = {'fallback-claude-opus-5-5[1m]': 'anthropic/claude-opus-5-5', 'fallback-claude-opus-5-5': 'anthropic/claude-opus-5-5', 'fallback-claude-opus-5[1m]': 'anthropic/claude-opus-5', 'fallback-claude-opus-5': 'anthropic/claude-opus-5', 'fallback-claude-opus-4-8[1m]': 'anthropic/claude-opus-4-8', 'fallback-claude-opus-4-8': 'anthropic/claude-opus-4-8', 'fallback-claude-opus-4-7[1m]': 'anthropic/claude-opus-4-8', 'fallback-claude-opus-4-7': 'anthropic/claude-opus-4-8', 'fallback-claude-sonnet-5-5[1m]': 'anthropic/claude-sonnet-5-5', 'fallback-claude-sonnet-5-5': 'anthropic/claude-sonnet-5-5', 'fallback-claude-sonnet-5[1m]': 'anthropic/claude-sonnet-5', 'fallback-claude-sonnet-5': 'anthropic/claude-sonnet-5', 'fallback-claude-sonnet-4-6': 'anthropic/claude-sonnet-5', 'fallback-claude-fable-5-1': 'anthropic/claude-fable-5-1', 'fallback-claude-haiku-5-5[1m]': 'anthropic/claude-haiku-5-5', 'fallback-claude-haiku-5-5': 'anthropic/claude-haiku-5-5', 'fallback-claude-haiku-4-5-20251001': 'anthropic/claude-haiku-4-5-20251001'}

_CLAUDE_FALLBACK_MODEL_GROUPS = set(_CLAUDE_FALLBACK_PROVIDER_MODELS)

_CLAUDE_ONE_MILLION_CONTEXT_MODELS = {'claude-opus-4-8[1m]', 'claude-opus-5[1m]', 'claude-opus-5-5[1m]', 'opus[1m]', 'claude-sonnet-5[1m]', 'claude-sonnet-5-5[1m]', 'claude-haiku-5-5[1m]'}

_CLAUDE_ONE_MILLION_CONTEXT_BETA = 'context-1m-2025-08-07'

_ANTHROPIC_API_BASE = 'https://api.anthropic.com'
