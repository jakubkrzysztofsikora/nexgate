# ruff: noqa
"""Local shims used by LiteLLM's file-based callback loader."""

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
    _original_load_credentials = getattr(CCProxyConfig, '_original_load_credentials', getattr(CCProxyConfig, '_load_credentials', None))
    if _original_load_credentials is not None:
        CCProxyConfig._original_load_credentials = _original_load_credentials

        def _patched_load_credentials(self):
            try:
                _original_load_credentials(self)
            except Exception as exc:
                if not hasattr(self, '_oat_values') or self._oat_values is None:
                    self._oat_values = {}
                if not hasattr(self, '_oat_user_agents') or self._oat_user_agents is None:
                    self._oat_user_agents = {}
                for provider in getattr(self, 'oat_sources', {}):
                    if provider not in self._oat_values:
                        self._oat_values[provider] = ''
        CCProxyConfig._load_credentials = _patched_load_credentials
except Exception:
    from ccproxy.config import get_config
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
        if 'Invalid content item type: image' in message:
            return False
        if 'failed to count tokens' in message and 'image' in message:
            return False
        return True

from ccproxy.config import CCProxyConfig, get_config
from ccproxy.handler import CCProxyHandler
from litellm.litellm_core_utils.get_llm_provider_logic import get_llm_provider
