import pytest

from gateway.anthropic_compatible import AnthropicCompatibleAdapter
from gateway.config import ConfigurationError, adapter_from_environment
from gateway.openai_compatible import OpenAICompatibleAdapter


def test_requires_explicit_provider() -> None:
    with pytest.raises(ConfigurationError, match="explicitly"):
        adapter_from_environment({})


def test_selects_openai_adapter() -> None:
    adapter = adapter_from_environment({"GATEWAY_PROVIDER": "openai-compatible", "OPENAI_COMPATIBLE_BASE_URL": "https://example.test", "OPENAI_COMPATIBLE_API_KEY": "fixture"})
    assert isinstance(adapter, OpenAICompatibleAdapter)


def test_selects_anthropic_adapter() -> None:
    adapter = adapter_from_environment({"GATEWAY_PROVIDER": "anthropic-compatible", "ANTHROPIC_COMPATIBLE_BASE_URL": "https://example.test", "ANTHROPIC_COMPATIBLE_API_KEY": "fixture"})
    assert isinstance(adapter, AnthropicCompatibleAdapter)


def test_does_not_echo_secret_in_configuration_error() -> None:
    with pytest.raises(ConfigurationError) as error:
        adapter_from_environment({"GATEWAY_PROVIDER": "unknown", "OPENAI_COMPATIBLE_API_KEY": "do-not-echo"})
    assert "do-not-echo" not in str(error.value)


def test_rejects_invalid_url_during_configuration() -> None:
    with pytest.raises(ConfigurationError, match="base URL is invalid"):
        adapter_from_environment({"GATEWAY_PROVIDER": "openai-compatible", "OPENAI_COMPATIBLE_BASE_URL": "ftp://example.test", "OPENAI_COMPATIBLE_API_KEY": "fixture"})
