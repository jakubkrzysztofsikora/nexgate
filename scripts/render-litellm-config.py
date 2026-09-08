#!/usr/bin/env python3
"""Render only configured LiteLLM routes from the portable model catalog."""

from __future__ import annotations

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
        return {key: resolve_environment(item, environment) for key, item in value.items()}
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


def main() -> int:
    dotenv_path = ROOT / os.environ.get("NEXGATE_ENV_FILE", ".env")
    if not dotenv_path.is_file():
        print("Run make configure first, then add credentials for the providers you want to use.", file=sys.stderr)
        return 2

    environment = load_dotenv(dotenv_path)
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
    print(f"Rendered {len(selected_models)} enabled model routes; skipped {skipped_models} incomplete routes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
