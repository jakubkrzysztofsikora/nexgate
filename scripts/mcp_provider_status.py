#!/usr/bin/env python3
"""NexGate - Integrated Subscription & Limits Monitor.

Tracks usage, live limits, and local timezone refresh times for:
1. Claude Subscriptions (1 & 2 / Opus, Sonnet, Haiku)
2. ChatGPT (ChatGPT subscription route)
3. MiniMax (MiniMax-M3)
4. Kimi (Moonshot / K3)
5. Z.ai GLM (GLM-5.1 & GLM-5.2)
6. QwenCloud (DashScope MaaS & PAYG)
7. Mistral (Mistral Vibe CLI)
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

# Provider Group Definitions & Keywords
PROVIDER_GROUPS = {
    "Claude Subs (1 & 2)": {
        "models": [
            "claude-opus-4-8",
            "claude-opus-4-8[1m]",
            "claude-sonnet-5",
            "claude-sonnet-5[1m]",
            "claude-haiku-4-5-20251001",
            "claude-fable-5",
            "claude-chat",
            "opus[1m]",
        ],
        "log_keywords": ["claude", "anthropic"],
        "probe_model": "claude-opus-4-8",
    },
    "ChatGPT": {
        "models": [
            "chatgpt/gpt-5.5",
            "chatgpt/gpt-5.6-sol",
            "chatgpt/gpt-5.6-terra",
            "chatgpt/gpt-5.6-luna",
        ],
        "log_keywords": ["chatgpt", "gpt-5.5", "gpt-5.6"],
        "probe_model": "chatgpt/gpt-5.5",
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
            "kimi",
            "moonshot/k3",
        ],
        "log_keywords": ["kimi", "moonshot"],
        "probe_model": "kimi-k3",
    },
    "Z.ai GLM": {
        "models": [
            "glm-5.1",
            "glm-5.2",
            "qwencloud/glm-5.2",
        ],
        "log_keywords": ["glm", "zai"],
        "probe_model": "glm-5.2",
    },
    "QwenCloud": {
        "models": [
            "qwencloud/qwen3.8-max",
            "qwencloud/qwen3.7-plus",
            "qwencloud/deepseek-v4-flash",
            "qwencloud-payg/qwen3.7-plus",
            "qwencloud-payg/qwen3.8-max",
            "qwencloud-payg/deepseek-v4-flash",
        ],
        "log_keywords": ["qwen", "dashscope"],
        "probe_model": "qwencloud-payg/qwen3.7-plus",
    },
    "Mistral": {
        "models": [
            "mistral",
            "mistral/mistral-vibe-cli-latest",
        ],
        "log_keywords": ["mistral"],
        "probe_model": "mistral",
    },
}

PROVIDER_5H_GUIDELINES = {
    "Claude Subs (1 & 2)": 150000,
    "ChatGPT": 200000,
    "MiniMax": 500000,
    "Kimi (Moonshot)": 200000,
    "Z.ai GLM": 300000,
    "QwenCloud": 500000,
    "Mistral": 200000,
}


def get_local_tz():
    return datetime.now().astimezone().tzinfo


def format_local_time(dt_utc: datetime) -> str:
    """Convert UTC datetime to local time string in local timezone."""
    if dt_utc is None:
        return "5h Rolling"
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=timezone.utc)
    local_dt = dt_utc.astimezone(get_local_tz())
    tz_name = local_dt.strftime("%Z") or local_dt.strftime("%z")
    return local_dt.strftime(f"%H:%M:%S {tz_name}")


def get_time_until(dt_utc: datetime) -> str:
    """Format time difference from now until target UTC datetime."""
    if dt_utc is None:
        return "Rolling"
    now_utc = datetime.now(timezone.utc)
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=timezone.utc)
    diff = dt_utc - now_utc
    if diff.total_seconds() <= 0:
        return "Now"
    hours, remainder = divmod(int(diff.total_seconds()), 3600)
    minutes, _ = divmod(remainder, 60)
    if hours > 0:
        return f"in {hours}h {minutes}m"
    return f"in {minutes}m"


def query_db(sql: str) -> list[dict]:
    """Execute SQL query against litellm_db PostgreSQL container."""
    cmd = [
        "docker",
        "exec",
        "litellm_db",
        "psql",
        "-U",
        "llmproxy",
        "-d",
        "litellm",
        "-A",
        "-t",
        "-F",
        "\t",
        "-c",
        sql,
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        lines = res.stdout.strip().splitlines()
        rows = []
        for line in lines:
            if line:
                rows.append(line.split("\t"))
        return rows
    except Exception:
        return []


def parse_container_logs(stats: dict[str, dict]):
    """Parse docker container logs for real-time rate limit/quota errors and reset timestamps."""
    cmd = ["docker", "logs", "--since=24h", "nexgate-agent-setup-litellm-1"]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        lines = res.stdout.splitlines() + res.stderr.splitlines()
    except Exception:
        return

    now_utc = datetime.now(timezone.utc)

    # Search backwards through container log
    for line in reversed(lines):
        line_lower = line.lower()
        if any(
            err in line_lower
            for err in [
                "error",
                "exception",
                "429",
                "403",
                "500",
                "quota",
                "limit",
                "exhausted",
                "usage_limit_reached",
            ]
        ):
            for group, config in PROVIDER_GROUPS.items():
                # Skip if already marked quota exhausted by a more recent log entry
                if stats[group]["status"] != "HEALTHY":
                    continue

                if any(kw in line_lower for kw in config["log_keywords"]):
                    if any(
                        term in line_lower
                        for term in [
                            "quota",
                            "exhausted",
                            "usage limit",
                            "usage_limit_reached",
                            "429",
                            "billing cycle",
                        ]
                    ):
                        stats[group]["status"] = "QUOTA EXHAUSTED"

                        # Clean up error message
                        clean_msg = re.sub(
                            r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}\S*\s*", "", line
                        )
                        # Extract core exception text if possible
                        if "APIError:" in clean_msg:
                            clean_msg = clean_msg.split("APIError:")[-1].strip()
                        elif "RateLimitError:" in clean_msg:
                            clean_msg = clean_msg.split("RateLimitError:")[-1].strip()

                        stats[group]["error_msg"] = clean_msg[:100]

                        # Check for explicit reset timestamp e.g. "reset at 08-05 16:20:00 UTC"
                        m = re.search(r"reset at (\d{2}-\d{2} \d{2}:\d{2}:\d{2}) UTC", line)
                        if m:
                            try:
                                time_part = m.group(1)
                                curr_year = now_utc.year
                                full_str = f"{curr_year}-{time_part}"
                                reset_dt = datetime.strptime(
                                    full_str, "%Y-%m-%d %H:%M:%S"
                                ).replace(tzinfo=timezone.utc)
                                if reset_dt > now_utc:
                                    stats[group]["reset_time_utc"] = reset_dt
                            except Exception:
                                pass


def probe_single_provider(item: tuple[str, str], master_key: str) -> tuple[str, str, str]:
    name, m = item
    payload = {
        "model": m,
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "hi"}],
    }
    req = urllib.request.Request(
        "http://127.0.0.1:4000/v1/messages",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "x-litellm-api-key": f"Bearer {master_key}",
            "anthropic-version": "2023-06-01",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            return name, "HEALTHY", "200 OK"
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        return name, "QUOTA EXHAUSTED", f"HTTP {e.code}: {body[:100]}"
    except Exception as exc:
        return name, "ERROR", str(exc)[:100]


def probe_all_providers(stats: dict[str, dict]):
    """Actively probes all 7 providers live via a parallel 1-token HTTP request."""
    master_key = os.getenv("LITELLM_MASTER_KEY", "")
    if not master_key:
        # Load from .env if present
        env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
        if os.path.exists(env_path):
            with open(env_path) as f:
                for line in f:
                    if line.startswith("LITELLM_MASTER_KEY="):
                        master_key = line.split("=", 1)[1].strip().strip('"').strip("'")
                        break

    items = [(group, config["probe_model"]) for group, config in PROVIDER_GROUPS.items()]
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(items)) as executor:
        futures = [executor.submit(probe_single_provider, item, master_key) for item in items]
        for f in concurrent.futures.as_completed(futures):
            name, st, msg = f.result()
            if st != "HEALTHY":
                stats[name]["status"] = st
                stats[name]["error_msg"] = msg


def fetch_usage_data(run_probe: bool = False) -> dict[str, dict]:
    """Fetch usage stats per provider for 5h and 24h windows."""
    stats = {}
    for name in PROVIDER_GROUPS:
        stats[name] = {
            "requests_5h": 0,
            "tokens_5h": 0,
            "requests_24h": 0,
            "tokens_24h": 0,
            "last_active": None,
            "status": "HEALTHY",
            "error_msg": None,
            "reset_time_utc": None,
        }

    # 1. Query PostgreSQL SpendLogs
    sql_logs = """
    SELECT model, model_group, custom_llm_provider, prompt_tokens, completion_tokens, "startTime"
    FROM "LiteLLM_SpendLogs"
    WHERE "startTime" >= NOW() - INTERVAL '24 hours'
    ORDER BY "startTime" DESC;
    """
    rows = query_db(sql_logs)
    now_utc = datetime.now(timezone.utc)
    five_hours_ago = now_utc - timedelta(hours=5)

    for row in rows:
        if len(row) < 6:
            continue
        model, model_group, provider, p_tok, c_tok, start_str = row[:6]
        try:
            p_tokens = int(p_tok or 0)
            c_tokens = int(c_tok or 0)
            tot_tokens = p_tokens + c_tokens
            dt = datetime.fromisoformat(start_str.replace(" ", "T"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except Exception:
            continue

        matched_group = None
        for grp_name, config in PROVIDER_GROUPS.items():
            models = config["models"]
            if model in models or model_group in models:
                matched_group = grp_name
                break
            if not matched_group and any(m in model for m in models):
                matched_group = grp_name
                break

        if matched_group:
            stats[matched_group]["requests_24h"] += 1
            stats[matched_group]["tokens_24h"] += tot_tokens
            if dt >= five_hours_ago:
                stats[matched_group]["requests_5h"] += 1
                stats[matched_group]["tokens_5h"] += tot_tokens
            if (
                stats[matched_group]["last_active"] is None
                or dt > stats[matched_group]["last_active"]
            ):
                stats[matched_group]["last_active"] = dt

    # 2. Parse Docker Container Logs for Error / Limit / Quota events
    parse_container_logs(stats)

    # 3. Active probe if requested
    if run_probe:
        probe_all_providers(stats)

    # 4. Fill default reset timestamps and heuristics
    for grp_name, data in stats.items():
        if data["reset_time_utc"] is None:
            if data["last_active"]:
                data["reset_time_utc"] = data["last_active"] + timedelta(hours=5)
            else:
                data["reset_time_utc"] = now_utc + timedelta(hours=5)

        guideline = PROVIDER_5H_GUIDELINES.get(grp_name, 200000)
        if data["status"] == "HEALTHY" and data["tokens_5h"] > guideline:
            data["status"] = "HEAVY USE"

    return stats


def generate_report(stats: dict[str, dict], markdown: bool = False, breakdown_only: bool = False) -> str:
    now_utc = datetime.now(timezone.utc)
    local_now = format_local_time(now_utc)
    out = []

    if breakdown_only or markdown:
        out.append(f"### 📊 NexGate Subscriptions Status Breakdown ({local_now})\n")
        for name, d in stats.items():
            st = d["status"]
            if st == "HEALTHY":
                status_str = "🟢 **HEALTHY**"
            elif st == "HEAVY USE":
                status_str = "🟡 **HEAVY USE / ACTIVE**"
            elif st == "RATE LIMITED":
                status_str = "⚠️ **RATE LIMITED**"
            else:
                status_str = "🔴 **QUOTA EXHAUSTED**"

            reset_str = format_local_time(d["reset_time_utc"]) if d["reset_time_utc"] else "5h Rolling"
            countdown = get_time_until(d["reset_time_utc"]) if d["reset_time_utc"] else "Rolling"
            
            line = f"* **{name}**: {status_str} — 5h tokens: {d['tokens_5h']:,} | 24h tokens: {d['tokens_24h']:,} | Next refresh: `{reset_str}` (`{countdown}`)"
            out.append(line)
            if d.get("error_msg"):
                out.append(f"  * *Detail*: `{d['error_msg']}`")
        return "\n".join(out)

    # ANSI Terminal output
    c_cyan = "\033[1;36m"
    c_green = "\033[1;32m"
    c_yellow = "\033[1;33m"
    c_red = "\033[1;31m"
    c_reset = "\033[0m"
    c_bold = "\033[1m"

    out.append(f"{c_cyan}═══ NexGate - Integrated Subscriptions & Limits Monitor ═══{c_reset}")
    out.append(f"Current Local Time: {c_bold}{local_now}{c_reset}\n")

    header = f"{'Provider / Subscription':<24} {'Status':<18} {'5h Req':<8} {'5h Tokens':<12} {'24h Tokens':<12} {'Next Refresh (Local)':<22} {'Countdown'}"
    out.append(f"{c_bold}{header}{c_reset}")
    out.append("─" * 105)

    for name, d in stats.items():
        st = d["status"]
        if st == "HEALTHY":
            status_color = f"{c_green}🟢 HEALTHY{c_reset}"
        elif st == "HEAVY USE":
            status_color = f"{c_yellow}🟡 HEAVY USE{c_reset}"
        elif st == "RATE LIMITED":
            status_color = f"{c_yellow}⚠️  LIMITED{c_reset}"
        else:
            status_color = f"{c_red}🔴 QUOTA EXHAUSTED{c_reset}"

        reset_str = format_local_time(d["reset_time_utc"]) if d["reset_time_utc"] else "5h Rolling"
        countdown = get_time_until(d["reset_time_utc"]) if d["reset_time_utc"] else "Rolling"

        row = f"{name:<24} {status_color:<27} {d['requests_5h']:<8} {d['tokens_5h']:<12,} {d['tokens_24h']:<12,} {reset_str:<22} {countdown}"
        out.append(row)
        if d.get("error_msg"):
            out.append(f"   ↳ {c_red}Limit Warning:{c_reset} {d['error_msg']}")

    out.append("─" * 105)
    return "\n".join(out)


def main():
    parser = argparse.ArgumentParser(description="Subscription status monitor")
    parser.add_argument("--json", action="store_true", help="Output raw JSON")
    parser.add_argument("--markdown", action="store_true", help="Output Markdown table")
    parser.add_argument("--breakdown", action="store_true", help="Output concise bulleted breakdown")
    parser.add_argument("--probe", action="store_true", help="Perform live 1-token active probe")
    args = parser.parse_args()

    stats = fetch_usage_data(run_probe=args.probe)
    if args.json:
        json_obj = {}
        for k, v in stats.items():
            json_obj[k] = dict(v)
            if json_obj[k]["last_active"]:
                json_obj[k]["last_active"] = json_obj[k]["last_active"].isoformat()
            if json_obj[k]["reset_time_utc"]:
                json_obj[k]["reset_time_utc"] = json_obj[k]["reset_time_utc"].isoformat()
                json_obj[k]["reset_time_local"] = format_local_time(v["reset_time_utc"])
                json_obj[k]["countdown"] = get_time_until(v["reset_time_utc"])
        print(json.dumps(json_obj, indent=2))
    else:
        print(generate_report(stats, markdown=args.markdown, breakdown_only=args.breakdown))


if __name__ == "__main__":
    main()
