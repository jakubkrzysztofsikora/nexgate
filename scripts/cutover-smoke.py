#!/usr/bin/env python3
"""Cutover smoke: probe the same provider models against sovereign (:4000)
and NexGate (:4001) side by side. Exit 1 if NexGate regresses vs sovereign.

Usage:
  python3 scripts/cutover-smoke.py [--old-port 4000] [--new-port 4001]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor

PROBES = [
    ("claude-opus-4-8", "claude"),
    ("claude-sonnet-5", "claude"),
    ("claude-haiku-4-5-20251001", "claude"),
    ("claude-fable-5-1", "claude"),
    ("opus[1m]", "claude"),
    ("chatgpt/gpt-5.6-terra", "chatgpt"),
    ("chatgpt/gpt-5.6-sol", "chatgpt"),
    ("minimax-m3", "minimax"),
    ("kimi-k3", "kimi"),
    ("glm-5.2", "zai"),
    ("glm-5.3", "zai"),
    ("qwencloud/qwen3.8-max", "qwencloud"),
    ("mistral", "mistral"),
    ("scaleway/gpt-oss", "scaleway"),
    ("deepseek-v4-pro", "deepseek"),
    ("bielik", "local"),
    ("gemma", "local"),
]


def probe(port: int, model: str, key: str, timeout: int) -> str:
    payload = json.dumps({
        "model": model,
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "hi"}],
    }).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/messages",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-litellm-api-key": f"Bearer {key}",
            "anthropic-version": "2023-06-01",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return "OK"
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:120]
        return f"HTTP {e.code} {body}"
    except Exception as exc:
        return f"ERR {exc}"[:120]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--old-port", type=int, default=4000)
    ap.add_argument("--new-port", type=int, default=4001)
    ap.add_argument("--timeout", type=int, default=25)
    args = ap.parse_args()

    key = os.environ.get("LITELLM_MASTER_KEY", "")
    if not key:
        print("LITELLM_MASTER_KEY not set — source .envrc first", file=sys.stderr)
        return 2

    def run(port: int) -> dict[str, str]:
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = {ex.submit(probe, port, m, key, args.timeout): m for m, _ in PROBES}
            return {m: f.result() for f, m in futs.items()}

    old = run(args.old_port)
    new = run(args.new_port)

    regressions = []
    print(f"{'model':<38} {'sovereign':<24} {'nexgate':<24}")
    for model, _ in PROBES:
        o = "OK" if old.get(model) == "OK" else old.get(model, "?")[:22]
        n = "OK" if new.get(model) == "OK" else new.get(model, "?")[:22]
        mark = "" if n == "OK" or o != "OK" else "  <-- REGRESSION"
        if mark:
            regressions.append(model)
        print(f"{model:<38} {o:<24} {n:<24}{mark}")

    if regressions:
        print(f"\nFAIL: {len(regressions)} NexGate regression(s) vs sovereign: {', '.join(regressions)}")
        print("Revert: point ANTHROPIC_BASE_URL back to :4000; leave old stack running.")
        return 1
    print("\nPASS: NexGate covers every provider that sovereign answers.")
    print("Safe to flip ANTHROPIC_BASE_URL to :4001, then `docker compose down` the old stack.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
