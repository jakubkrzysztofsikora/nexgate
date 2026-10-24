"""Config-assertion tests for NexGate fallback routing (TC-012..015, TC-023).

Ground truth: the rendered runtime/state/litellm.yaml. Live-behavior stubs
(TC-010 forced-failure) are skip-marked until a dry-run gateway env exists.
"""
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "runtime/state/litellm.yaml"


def _load_config():
    return yaml.safe_load(CONFIG_PATH.read_text())


def _fallback_map(config, key):
    entries = config["router_settings"][key]
    assert isinstance(entries, list), f"{key} must be a list of single-key dicts"
    for item in entries:
        assert isinstance(item, dict) and len(item) == 1, f"bad fallback entry: {item}"
    return {k: v for entry in entries for k, v in entry.items()}


def test_tc012_terminal_groups_have_explicit_empty_fallbacks():
    config = _load_config()
    fallbacks = _fallback_map(config, "fallbacks")
    cwf = _fallback_map(config, "context_window_fallbacks")
    for group in ("coopforge/bielik-minitron-7b", "circit-gateway", "claude-haiku-4-5-20251001"):
        assert group in fallbacks, f"{group} missing explicit fallback rule"
        assert fallbacks[group] == [], f"{group} must be terminal-empty"
    for group in ("bielik", "psyllm-4b", "circit-gateway", "glm-5.1", "opus[1m]"):
        assert group in cwf, f"{group} missing explicit cwf rule"
        assert cwf[group] == [], f"{group} cwf must be terminal-empty"


def test_tc015_default_fallbacks_order():
    config = _load_config()
    assert config["router_settings"]["default_fallbacks"] == [
        "kimi-k3",
        "qwencloud/qwen3.7-plus",
        "glm-5.3-flash",
        "glm-5.3",
        "mistral/glm-5.2",
        "minimax-m3",
        "mistral",
    ]


def test_tc014_gemma_context_window_fallbacks():
    config = _load_config()
    cwf = _fallback_map(config, "context_window_fallbacks")
    assert cwf["gemma"] == ["minimax-m3", "scaleway/gpt-oss"]


def test_tc013_claude_context_overflow_escalates_to_1m_then_terminal():
    config = _load_config()
    cwf = _fallback_map(config, "context_window_fallbacks")
    fallbacks = _fallback_map(config, "fallbacks")
    escalations = {
        "claude-opus-4-8": "claude-opus-4-8[1m]",
        "claude-opus-4-7": "claude-opus-4-7[1m]",
        "claude-opus-5": "claude-opus-5[1m]",
        "claude-opus-5-5": "claude-opus-5-5[1m]",
        "claude-sonnet-5-5": "claude-sonnet-5-5[1m]",
        "claude-sonnet-5": "claude-sonnet-5[1m]",
        "claude-sonnet-4-6": "claude-sonnet-5[1m]",
        "claude-haiku-4-5-20251001": "claude-haiku-5-5[1m]",
        "claude-haiku-5-5": "claude-haiku-5-5[1m]",
    }
    for base, escalated in escalations.items():
        assert cwf[base] == [escalated], f"{base} must escalate to {escalated}"
        assert cwf[escalated] == [], f"{escalated} cwf must be terminal"
        assert fallbacks[escalated] == [], f"{escalated} fallbacks must be terminal"


def test_tc023_minimax_m3_flash_inherits_default_fallbacks():
    config = _load_config()
    fallbacks = _fallback_map(config, "fallbacks")
    cwf = _fallback_map(config, "context_window_fallbacks")
    assert "minimax-m3-flash" not in fallbacks
    assert "minimax-m3-flash" not in cwf
    assert config["router_settings"]["default_fallbacks"]


@pytest.mark.skip(reason="TC-010 needs a dry-run gateway env with a bogus-key deployment; upstream quota outage blocks live runs (2026-10-24)")
def test_tc010_forced_failure_propagates_without_wildcard():
    pass
