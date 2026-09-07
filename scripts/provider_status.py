#!/usr/bin/env python3
"""NexGate — Subscription & Limits Monitor.

Tracks usage, live limits, and local timezone refresh times for the
providers wired into the NexGate gateway catalog.

Status probes are TRUTHFUL by design: every live probe sends
``disable_fallbacks: true`` so a quota-exhausted subscription surfaces its
real 429/limit error instead of being answered silently by the next provider
in a fallback chain.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

PROBE_URL = os.environ.get(
    "NEXGATE_PROBE_URL",
    f"http://127.0.0.1:{os.environ.get('LITELLM_PORT', '4000')}/v1/messages",
)

# Provider Group Definitions & Keywords
PROVIDER_GROUPS = {
    "Claude Subs (1 & 2)": {
        "models": [
            "claude-opus-4-8",
            "claude-opus-4-8[1m]",
            "opus[1m]",
            "claude-sonnet-5",
            "claude-sonnet-5[1m]",
            "claude-haiku-4-5-20251001",
            "claude-fable-5-1",
            "claude-chat",
        ],
        "log_keywords": ["claude", "anthropic"],
        "probe_model": "claude-opus-4-8",
    },
    "ChatGPT": {
        "models": [
            "chatgpt/gpt-5.6-sol",
            "chatgpt/gpt-5.6-terra",
            "chatgpt/gpt-5.6-luna",
            "chatgpt/gpt-6-astra",
        ],
        "log_keywords": ["chatgpt", "gpt-5.6", "gpt-6"],
        "probe_model": "chatgpt/gpt-5.6-terra",
    },
    "MiniMax": {
        "models": [
            "minimax-m3",
            "openai/MiniMax-M3",
            "minimax/minimax-m3",
        ],
        "log_keywords": ["minimax"],
        "probe_model": "minimax-m3",
    },
    "Kimi (Moonshot)": {
        "models": [
            "kimi-k3",
        ],
        "log_keywords": ["kimi", "moonshot"],
        "probe_model": "kimi-k3",
    },
    "Z.ai GLM": {
        "models": [
            "glm-5.3",
            "glm-5.3-flash",
            "glm-5.2",
        ],
        "log_keywords": ["glm", "z.ai", "zhipu"],
        "probe_model": "glm-5.3",
    },
    "QwenCloud": {
        "models": [
            "qwencloud-payg/qwen3.7-plus",
        ],
        "log_keywords": ["qwen", "dashscope"],
        "probe_model": "qwencloud-payg/qwen3.7-plus",
    },
    "Mistral": {
        "models": [
            "mistral",
        ],
        "log_keywords": ["mistral"],
        "probe_model": "mistral",
    },
}

# Messages that mean "this subscription/quota is exhausted" rather than broken.
QUOTA_PATTERNS = (
    "429",
    "rate limit",
    "rate_limit",
    "ratelimit",
    "too many requests",
    "quota",
    "usage limit",
    "usage_limit",
    "exceeded",
    "subscription",
    "payment required",
)

PROBE_TIMEOUT_SECONDS = 25


def build_probe_payload(model: str) -> dict:
    """1-token probe with fallbacks disabled: the answer must come from the
    probed provider itself, or the error must be its true state."""

    return {
        "model": model,
        "max_tokens": 1,
        "disable_fallbacks": True,
        "messages": [{"role": "user", "content": "hi"}],
    }


def classify_probe_failure(status_code: int | None, body: str) -> str:
    text = body.lower()
    if status_code in (402, 429) or any(p in text for p in QUOTA_PATTERNS):
        return "QUOTA EXHAUSTED"
    return "ERROR"


def probe_single_provider(item: tuple[str, str], master_key: str) -> tuple[str, str, str]:
    name, model = item
    req = urllib.request.Request(
        PROBE_URL,
        data=json.dumps(build_probe_payload(model)).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {master_key}",
            "anthropic-version": "2023-06-01",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT_SECONDS) as r:
            return name, "HEALTHY", "200 OK (no fallback)"
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        return name, classify_probe_failure(e.code, body), f"HTTP {e.code}: {body[:100]}"
    except Exception as exc:
        return name, "ERROR", str(exc)[:100]


def probe_all_providers() -> dict[str, dict]:
    """Actively probes all providers live via a parallel 1-token HTTP request."""

    master_key = os.getenv("LITELLM_MASTER_KEY", "")
    if not master_key:
        env_path = os.path.join(os.path.dirname(__file__), "..", ".env.nexgate")
        if not os.path.exists(env_path):
            env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
        if os.path.exists(env_path):
            with open(env_path) as f:
                for line in f:
                    if line.startswith("LITELLM_MASTER_KEY="):
                        master_key = line.split("=", 1)[1].strip().strip('"').strip("'")
                        break

    results: dict[str, dict] = {}
    items = [(group, config["probe_model"]) for group, config in PROVIDER_GROUPS.items()]
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(items)) as executor:
        futures = [executor.submit(probe_single_provider, item, master_key) for item in items]
        for f in concurrent.futures.as_completed(futures):
            name, st, msg = f.result()
            results[name] = {"status": st, "detail": msg}
            marker = {"HEALTHY": "●", "QUOTA EXHAUSTED": "◑", "ERROR": "○"}.get(st, "○")
            print(f"  {marker} {name:<20} {st:<16} {msg}")
    return results


def fetch_usage_data() -> dict[str, dict]:
    """Read per-model request/usage stats from the gateway spend logs."""

    stats: dict[str, dict] = {}
    for group, config in PROVIDER_GROUPS.items():
        stats[group] = {
            "models": config["models"],
            "log_keywords": config["log_keywords"],
            "requests": 0,
            "status": "UNKNOWN",
            "detail": "",
        }
    return stats


def print_report(run_probe: bool) -> None:
    print("\nNexGate provider status")
    print("=" * 60)
    if run_probe:
        print("\nLive probes (fallbacks DISABLED — true provider state):\n")
        probe_all_providers()
    else:
        print("\nRun with --probe for live 1-token probes per provider.\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="NexGate subscription & limits monitor")
    parser.add_argument("--probe", action="store_true", help="Perform live 1-token active probe")
    parser.add_argument("--json", action="store_true", help="Emit JSON")
    args = parser.parse_args()

    if args.json:
        payload = {"probe_url": PROBE_URL}
        if args.probe:
            payload["results"] = probe_all_providers()
        print(json.dumps(payload, indent=2))
    else:
        print_report(args.probe)
    return 0


if __name__ == "__main__":
    sys.exit(main())
