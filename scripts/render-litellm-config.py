#!/usr/bin/env python3
"""Render only configured LiteLLM routes from the portable model catalog."""

from __future__ import annotations

import copy
import os
import re
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
PLACEHOLDER_PREFIX = "replace-with-"
ENVIRONMENT_VALUE = re.compile(r"^os\.environ/([A-Za-z_][A-Za-z0-9_]*)$")
SUBSCRIPTION_ROUTES_FLAG = "NEXGATE_ENABLE_SUBSCRIPTION_ROUTES"


def load_dotenv(path: Path) -> dict[str, str]:
    # The generated config must reflect only the explicit overlay, never an
    # accidentally inherited shell credential.
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


def configured(value: str | None) -> bool:
    return bool(value) and PLACEHOLDER_PREFIX not in value


def referenced_environment(value: Any) -> set[str]:
    if isinstance(value, str):
        match = ENVIRONMENT_VALUE.fullmatch(value)
        return {match.group(1)} if match else set()
    if isinstance(value, list):
        return set().union(*(referenced_environment(item) for item in value))
    if isinstance(value, dict):
        return set().union(*(referenced_environment(item) for item in value.values()))
    return set()


def resolve_environment(value: Any, environment: dict[str, str]) -> Any:
    if isinstance(value, str):
        match = ENVIRONMENT_VALUE.fullmatch(value)
        if match and configured(environment.get(match.group(1))):
            return environment[match.group(1)]
        return value
    if isinstance(value, list):
        return [resolve_environment(item, environment) for item in value]
    if isinstance(value, dict):
        return {
            key: resolve_environment(item, environment) for key, item in value.items()
        }
    return value


def requires_subscription_opt_in(model: dict[str, Any]) -> bool:
    """Keep OAuth-backed and virtual aliases inert until the operator enables them."""
    return not referenced_environment(model)


def prune_chains(catalog: dict[str, Any], available: set[str]) -> None:
    """Drop fallback hops whose model was not rendered.

    Only `model_list` is filtered by configuration, so a chain written for the
    full catalog would otherwise send a failing request to a model group this
    deployment never defined — spending a retry to arrive at the same error.
    """
    router = catalog.get("router_settings") or {}
    resolvable = available | set(router.get("model_group_alias") or {}) | {"*"}
    for key in ("fallbacks", "context_window_fallbacks", "content_policy_fallbacks"):
        entries = []
        for entry in router.get(key) or []:
            kept = {
                source: [target for target in (chain or []) if target in resolvable]
                for source, chain in entry.items()
                if source in resolvable
            }
            entries.extend({source: chain} for source, chain in kept.items())
        if key in router:
            router[key] = entries
    if "default_fallbacks" in router:
        router["default_fallbacks"] = [
            target for target in router["default_fallbacks"] if target in resolvable
        ]
    for model in catalog.get("model_list") or []:
        params = model.get("litellm_params") or {}
        config = params.get("complexity_router_config")
        if not isinstance(config, dict):
            continue
        tiers = config.get("tiers")
        if isinstance(tiers, dict):
            config["tiers"] = {
                tier: [m for m in models if m in resolvable]
                for tier, models in tiers.items()
                if any(m in resolvable for m in models)
            }
        tier_configs = config.get("tier_model_configs")
        if isinstance(tier_configs, dict):
            config["tier_model_configs"] = {
                tier: [entry for entry in entries
                       if not isinstance(entry, dict)
                       or str(entry.get("model_name") or "") in resolvable]
                for tier, entries in tier_configs.items()
            }
        default = str(config.get("complexity_router_default_model") or params.get("complexity_router_default_model") or "")
        survivors = [m for models in (config.get("tiers") or {}).values() for m in models]
        if default and default not in resolvable and survivors:
            replacement = survivors[0]
            for target in (config, params):
                if isinstance(target, dict) and "complexity_router_default_model" in target:
                    target["complexity_router_default_model"] = replacement


def main() -> int:
    dotenv_name = os.environ.get("NEXGATE_ENV_FILE")
    if dotenv_name:
        dotenv_path = ROOT / dotenv_name
    elif (ROOT / ".env").is_file():
        dotenv_path = ROOT / ".env"
    elif (ROOT / ".env.nexgate").is_file():
        dotenv_path = ROOT / ".env.nexgate"
    else:
        dotenv_path = ROOT / ".env"
    if not dotenv_path.is_file():
        print(
            "Run make configure first, then add credentials for the providers you want to use.",
            file=sys.stderr,
        )
        return 2

    environment = load_dotenv(dotenv_path)
    if configured(environment.get("CCPROXY_ANTHROPIC_OAUTH_TOKEN_FILE")) and not configured(environment.get("CLAUDE_CODE_OAUTH_TOKEN")):
        environment["CLAUDE_CODE_OAUTH_TOKEN"] = "sk-ant-oat-file-placeholder"
    catalog = yaml.safe_load((ROOT / "runtime/config/litellm.yaml.tmpl").read_text())
    selected_models = []
    skipped_models = 0
    for model in catalog.get("model_list", []):
        required = referenced_environment(model)
        enabled = all(configured(environment.get(name)) for name in required)
        if requires_subscription_opt_in(model):
            enabled = environment.get(SUBSCRIPTION_ROUTES_FLAG, "").lower() == "true"
        if enabled:
            selected_models.append(resolve_environment(model, environment))
        else:
            skipped_models += 1
    catalog["model_list"] = selected_models
    prune_chains(catalog, {model["model_name"] for model in selected_models})
    catalog = resolve_environment(catalog, environment)

    output_path = ROOT / "runtime/state/litellm.yaml"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(yaml.safe_dump(catalog, sort_keys=False))

    # Also render the stateless configuration for Scaleway Serverless Fallback
    fallback_catalog = copy.deepcopy(catalog)
    fallback_general = fallback_catalog.get("general_settings", {})
    fallback_general.pop("database_url", None)
    fallback_general.pop("database_connection_pool_limit", None)
    fallback_general["store_model_in_db"] = False
    fallback_general["store_prompts_in_spend_logs"] = False

    fallback_litellm = fallback_catalog.get("litellm_settings", {})
    fallback_litellm["cache"] = False
    fallback_litellm.pop("cache_params", None)

    # Filter out models pointing to local machine endpoints (127.0.0.1 / localhost)
    fallback_models = []
    for model in fallback_catalog.get("model_list", []):
        params = model.get("litellm_params", {})
        api_base = str(params.get("api_base") or "")
        if "127.0.0.1" in api_base or "localhost" in api_base:
            continue
        fallback_models.append(model)
    fallback_catalog["model_list"] = fallback_models
    prune_chains(fallback_catalog, {m["model_name"] for m in fallback_models})

    fallback_output_path = ROOT / "runtime/state/litellm.fallback.yaml"
    fallback_output_path.write_text(yaml.safe_dump(fallback_catalog, sort_keys=False))

    print(
        f"Rendered {len(selected_models)} enabled model routes ({len(fallback_models)} fallback); skipped {skipped_models} incomplete routes."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
