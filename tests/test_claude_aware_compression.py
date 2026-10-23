"""Regression tests for the Claude-aware compression safety valve.

The callback used to run with absolute thresholds (trigger=100 tokens,
target=50) and no environment override, so every follow-up request was
compressed: agent history was shredded down to the first message plus a
handful of recent turns. Claude Code then re-read files it had already
edited and burned hours re-deriving state it had already handled -- the
"lobotomized" behaviour observed on claude-opus-4-8 in production.

These tests pin the last-resort semantics:
  * requests that fit the window pass through untouched;
  * budgets scale with the model's context window;
  * the non-message payload (system prompt + tool catalog) is reserved;
  * oversized requests keep most of their recent history, not a stub;
  * absolute overrides remain available for tests and controlled rollouts.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

from conftest import install_stub

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "runtime/config/claude_aware_compression.py"


class _CallTypes:
    anthropic_messages = "anthropic_messages"
    acompletion = "acompletion"
    completion = "completion"


def _char_counter(model: str = "", text=None, messages=None, **kwargs) -> int:
    """Deterministic stand-in for litellm's token_counter (~4 chars/token)."""
    if text is not None:
        return max(1, len(text) // 4)
    return max(1, len(json.dumps(messages or [], default=str)) // 4)


def _load_module(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> types.ModuleType:
    """Load the callback against permissive litellm stubs (mirrors the
    pattern used for the other runtime/config modules)."""
    install_stub(monkeypatch, "litellm")
    install_stub(monkeypatch, "litellm.integrations")
    install_stub(monkeypatch, "litellm.integrations.custom_logger", CustomLogger=object)
    install_stub(monkeypatch, "litellm.litellm_core_utils")
    install_stub(
        monkeypatch,
        "litellm.litellm_core_utils.token_counter",
        token_counter=_char_counter,
    )
    install_stub(monkeypatch, "litellm.types")
    install_stub(monkeypatch, "litellm.types.utils", CallTypes=_CallTypes)
    # Deterministic environment: no absolute overrides and no operator
    # overrides inherited from the shell (e.g. a local
    # CLAUDE_COMPRESSION_DROP_OLD_MESSAGES=0 experiment), savings log in tmp.
    for name in (
        "CLAUDE_COMPRESSION_ENABLED",
        "CLAUDE_COMPRESSION_TRIGGER",
        "CLAUDE_COMPRESSION_TARGET",
        "CLAUDE_COMPRESSION_TRIGGER_PCT",
        "CLAUDE_COMPRESSION_TARGET_PCT",
        "CLAUDE_COMPRESSION_TOKEN_SAFETY",
        "CLAUDE_COMPRESSION_MAX_TOOL_RESULT_CHARS",
        "CLAUDE_COMPRESSION_MIN_RECENT_MESSAGES",
        "CLAUDE_COMPRESSION_DROP_OLD_IMAGES",
        "CLAUDE_COMPRESSION_DROP_OLD_MESSAGES",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CLAUDE_COMPRESSION_SAVINGS_LOG", str(tmp_path / "savings.jsonl"))

    spec = importlib.util.spec_from_file_location(
        "claude_aware_compression_under_test", MODULE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def compression(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> types.ModuleType:
    return _load_module(monkeypatch, tmp_path)


def _conversation(pairs: int, chars: int = 400) -> list[dict]:
    messages = []
    for _ in range(pairs):
        messages.append({"role": "user", "content": [{"type": "text", "text": "u" * chars}]})
        messages.append({"role": "assistant", "content": [{"type": "text", "text": "a" * chars}]})
    messages.append({"role": "user", "content": [{"type": "text", "text": "final question"}]})
    return messages


def _request(module: types.ModuleType, messages: list[dict], **extra) -> dict:
    return {
        "model": "claude-opus-4-8",
        "messages": messages,
        "custom_llm_provider": "anthropic",
        **extra,
    }


def test_small_request_passes_through_untouched(compression: types.ModuleType) -> None:
    comp = compression.claude_aware_compression
    data = _request(compression, _conversation(10))
    assert comp._compress(dict(data), call_type="anthropic_messages") is None


def test_thresholds_scale_with_context_window(compression: types.ModuleType) -> None:
    comp = compression.ClaudeAwareCompression()
    # opus-4-8 window (1M): 90% trigger / 75% target.
    assert comp._effective_thresholds("claude-opus-4-8", None, False) == (900_000, 750_000)
    # haiku window (200k).
    assert comp._effective_thresholds("claude-haiku-4-5-20251001", None, False) == (
        180_000,
        150_000,
    )
    # The opening request of a session passes through while it fits the window.
    assert comp._effective_thresholds("claude-opus-4-8", None, True) == (1_000_000, 750_000)


def test_absolute_override_still_fires(compression: types.ModuleType) -> None:
    comp = compression.ClaudeAwareCompression(
        compression_trigger=100, compression_target=50
    )
    data = _request(compression, _conversation(10))
    assert comp._compress(data, call_type="anthropic_messages") is not None


def test_oversized_request_keeps_history_instead_of_shredding(
    compression: types.ModuleType,
) -> None:
    comp = compression.claude_aware_compression
    messages = _conversation(1500, chars=2000)  # ~1.5M estimated tokens
    out = comp._compress(_request(compression, messages), call_type="anthropic_messages")
    assert out is not None
    kept = out["messages"]
    # The old behaviour kept ~7 messages (first + a few recent turns); the
    # safety valve must retain the bulk of the recent history instead.
    assert len(kept) > 500
    assert kept[0] is messages[0]
    final_tokens = comp._safe_token_count("claude-opus-4-8", kept)
    # 75% of the 1M window, divided by the 1.35 tokenizer-calibration factor:
    # the kept set must land near that target, not merely "not catastrophic"
    # (the old shredded behaviour would keep ~7 messages for ~4k tokens).
    assert 450_000 <= final_tokens <= 560_000


def test_tool_catalog_is_reserved_from_the_message_budget(
    compression: types.ModuleType,
) -> None:
    comp = compression.claude_aware_compression
    # ~510k estimated tokens of messages: under the calibrated trigger alone.
    assert comp._compress(
        _request(compression, _conversation(500, chars=2000)),
        call_type="anthropic_messages",
    ) is None
    # The same conversation plus a large tool catalog must still be compressed:
    # the catalog is not shrunk by this callback, so it is reserved up front.
    tools = [{"name": f"tool_{i}", "description": "x" * 4000} for i in range(200)]
    data = _request(compression, _conversation(500, chars=2000), tools=tools)
    assert comp._overhead_tokens("claude-opus-4-8", data) > 150_000
    assert comp._compress(data, call_type="anthropic_messages") is not None
