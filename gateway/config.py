"""Explicit opt-in adapter selection from an untracked local environment."""

from __future__ import annotations

import os
from typing import Mapping

from gateway.anthropic_compatible import AnthropicCompatibleAdapter, AnthropicCompatibleConfig
from gateway.openai_compatible import OpenAICompatibleAdapter, OpenAICompatibleConfig


class ConfigurationError(ValueError):
    pass


def adapter_from_environment(environment: Mapping[str, str] | None = None):
    env = os.environ if environment is None else environment
    provider = env.get("GATEWAY_PROVIDER", "")
    if not provider:
        raise ConfigurationError("GATEWAY_PROVIDER must explicitly select an enabled adapter")
    if provider == "openai-compatible":
        base_url, api_key = env.get("OPENAI_COMPATIBLE_BASE_URL", ""), env.get("OPENAI_COMPATIBLE_API_KEY", "")
        if not base_url or not api_key:
            raise ConfigurationError("openai-compatible requires OPENAI_COMPATIBLE_BASE_URL and OPENAI_COMPATIBLE_API_KEY")
        config = OpenAICompatibleConfig(base_url, api_key)
        try:
            config.chat_completions_url()
        except ValueError as exc:
            raise ConfigurationError("openai-compatible base URL is invalid") from exc
        return OpenAICompatibleAdapter(config)
    if provider == "anthropic-compatible":
        base_url, api_key = env.get("ANTHROPIC_COMPATIBLE_BASE_URL", ""), env.get("ANTHROPIC_COMPATIBLE_API_KEY", "")
        if not base_url or not api_key:
            raise ConfigurationError("anthropic-compatible requires ANTHROPIC_COMPATIBLE_BASE_URL and ANTHROPIC_COMPATIBLE_API_KEY")
        config = AnthropicCompatibleConfig(base_url, api_key)
        try:
            config.messages_url()
        except ValueError as exc:
            raise ConfigurationError("anthropic-compatible base URL is invalid") from exc
        return AnthropicCompatibleAdapter(config)
    raise ConfigurationError("GATEWAY_PROVIDER is not an approved public adapter")
