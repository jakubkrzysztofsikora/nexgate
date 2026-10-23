#!/usr/bin/env python3
"""Install an explicit OMP catalog from LiteLLM metadata, with recoverable backups."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import urllib.request

import yaml


def build_models(catalog: dict, base_url: str) -> list[dict]:
    models = {}
    for entry in catalog["data"]:
        name = entry.get("model_name")
        info = entry.get("model_info") or {}
        params = entry.get("litellm_params") or {}
        backend = str(params.get("model") or "")
        if not name or name.startswith("fallback-") or name == "llm-422-sink":
            continue
        if info.get("mode") not in (None, "chat", "responses"):
            continue
        # 'openai/*' often means a third-party Chat Completions adapter.
        # Only a native Responses deployment should use OMP's Responses client.
        if backend.startswith("anthropic/"):
            api = "anthropic-messages"
        elif backend.startswith("chatgpt/") or info.get("mode") == "responses":
            api = "openai-responses"
        else:
            api = "openai-completions"
        context = info.get("max_input_tokens") or 128000
        output = min(info.get("max_output_tokens") or 32768, context)
        models[name] = {
            "id": name, "name": name, "api": api,
            "baseUrl": base_url.removesuffix("/v1") if api == "anthropic-messages" else base_url,
            "contextWindow": context, "maxTokens": output,
            "reasoning": bool(info.get("supports_reasoning")),
            "input": ["text", "image"] if info.get("supports_vision") else ["text"],
            "supportsTools": info.get("supports_function_calling") is not False,
        }
    return sorted(models.values(), key=lambda model: model["id"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:4000/v1")
    parser.add_argument("--agent-dir", type=Path, default=Path.home() / ".omp/agent")
    parser.add_argument("--catalog", type=Path, help="Use saved /model/info JSON instead of fetching")
    parser.add_argument("--key-env", default="NEXGATE_OMP_API_KEY")
    args = parser.parse_args()
    key = os.environ.get(args.key_env)
    agent = args.agent_dir.expanduser()
    existing_env = agent / ".env"
    if not key and existing_env.exists():
        for line in existing_env.read_text().splitlines():
            if line.startswith(args.key_env + "="):
                value = line.split("=", 1)[1]
                key = json.loads(value) if value.startswith('"') else value
    if not key:
        parser.error(f"Set {args.key_env} to an existing gateway client key")
    base_url = args.base_url.rstrip("/")
    if args.catalog:
        catalog = json.loads(args.catalog.read_text())
    else:
        request = urllib.request.Request(base_url.removesuffix("/v1") + "/model/info",
                                         headers={"Authorization": "Bearer " + key})
        with urllib.request.urlopen(request, timeout=30) as response:
            catalog = json.load(response)
    models = build_models(catalog, base_url)
    if not models:
        parser.error("Gateway returned no usable chat models")
    agent.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")

    def backup(path: Path) -> None:
        if path.exists():
            saved = path.with_name(path.name + ".backup-" + stamp)
            shutil.copy2(path, saved)
            saved.chmod(0o600)

    def write(path: Path, text: str) -> None:
        backup(path)
        temporary = path.with_name(path.name + ".tmp-" + stamp)
        with open(temporary, "x", opener=lambda p, flags: os.open(p, flags, 0o600)) as stream:
            stream.write(text)
        temporary.replace(path)

    models_path = agent / "models.yml"
    config = yaml.safe_load(models_path.read_text()) if models_path.exists() else {}
    config = config or {}
    config.setdefault("providers", {})["nexgate"] = {
        "baseUrl": base_url, "apiKey": args.key_env,
        "api": "openai-completions", "models": models,
    }
    write(models_path, yaml.safe_dump(config, sort_keys=False))
    settings_path = agent / "config.yml"
    settings = yaml.safe_load(settings_path.read_text()) if settings_path.exists() else {}
    settings = settings or {}
    roles = settings.setdefault("modelRoles", {})
    for role, value in roles.items():
        if isinstance(value, str) and value.startswith(("litellm/", "litellm-chat/")):
            roles[role] = "nexgate/" + value.split("/", 1)[1]
    roles.setdefault("default", "nexgate/glm-5.3")
    enabled = settings.get("enabledProviders", [])
    settings["enabledProviders"] = [p for p in enabled if p not in ("litellm", "litellm-chat", "nexgate")] + ["nexgate"]
    write(settings_path, yaml.safe_dump(settings, sort_keys=False))
    env_path = agent / ".env"
    env_lines = env_path.read_text().splitlines() if env_path.exists() else []
    env_lines = [line for line in env_lines if not line.startswith(args.key_env + "=")]
    env_lines.append(args.key_env + "=" + json.dumps(key))
    write(env_path, "\n".join(env_lines) + "\n")
    extensions = agent / "extensions"
    extensions.mkdir(exist_ok=True)
    write(extensions / "nexgate-history-guard.js", Path(__file__).with_name("omp-history-guard.mjs").read_text())
    print(f"Configured {len(models)} models in {agent}: {dict(Counter(m['api'] for m in models))}")
    print("Existing role selections preserved; litellm role references now use nexgate. Restart OMP to load configuration.")


if __name__ == "__main__":
    main()
