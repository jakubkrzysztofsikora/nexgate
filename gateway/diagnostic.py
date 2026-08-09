"""Bounded manual probe for an explicitly selected, private adapter overlay."""

from __future__ import annotations

import os
import sys

from gateway.config import ConfigurationError, adapter_from_environment
from gateway.openai_compatible import UpstreamError


MAX_TIMEOUT_SECONDS = 10.0


def _timeout_from_environment(environment: dict[str, str]) -> float:
    raw_timeout = environment.get("GATEWAY_DIAGNOSTIC_TIMEOUT_SECONDS", "10")
    try:
        timeout = float(raw_timeout)
    except ValueError as exc:
        raise ConfigurationError("GATEWAY_DIAGNOSTIC_TIMEOUT_SECONDS must be a number") from exc
    if not 0 < timeout <= MAX_TIMEOUT_SECONDS:
        raise ConfigurationError("diagnostic timeout must be greater than zero and at most 10 seconds")
    return timeout


def main() -> int:
    environment = dict(os.environ)
    if environment.get("CI"):
        print("adapter diagnostic is disabled in CI", file=sys.stderr)
        return 2
    if environment.get("GATEWAY_DIAGNOSTIC_CONFIRM") != "run":
        print("set GATEWAY_DIAGNOSTIC_CONFIRM=run to confirm one local probe", file=sys.stderr)
        return 2

    try:
        timeout = _timeout_from_environment(environment)
        adapter = adapter_from_environment(environment, timeout_seconds=timeout)
        model = environment["GATEWAY_DIAGNOSTIC_MODEL"]
        if environment["GATEWAY_PROVIDER"] == "anthropic-compatible":
            adapter.complete({"model": model, "max_tokens": 1, "messages": [{"role": "user", "content": "ping"}]})
        else:
            adapter.complete({"model": model, "max_tokens": 1, "messages": [{"role": "user", "content": "ping"}]})
    except KeyError:
        print("GATEWAY_DIAGNOSTIC_MODEL is required", file=sys.stderr)
        return 2
    except Exception:  # The diagnostic must not serialize local overlay details.
        # Do not print configuration, credentials, prompts, or provider bodies.
        print("adapter diagnostic failed; inspect the provider locally without sharing its response", file=sys.stderr)
        return 1

    print("adapter diagnostic passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
