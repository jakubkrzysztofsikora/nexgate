"""Shared stubbing helper for tests that load runtime/config modules standalone.

runtime/config/ccproxy_callback.py and sitecustomize.py import heavy
third-party packages (litellm, ccproxy) at module scope. Tests load them via
importlib in isolation and need throwaway stand-ins in sys.modules.
`install_stub` creates one, tolerant of arbitrary attribute access, and
relies on `monkeypatch` to revert it after the test so stubs never leak into
other test files.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import types
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent


class _Placeholder:
    """Attribute/callable stand-in for any unset stub attribute."""

    def __call__(self, *_args: object, **_kwargs: object) -> "_Placeholder":
        return _Placeholder()

    def __getattr__(self, name: str) -> "_Placeholder":
        return _Placeholder()

    def __iter__(self):
        return iter(())

    def __bool__(self) -> bool:
        return False


class _PermissiveModule(types.ModuleType):
    def __getattr__(self, name: str) -> object:
        if name.startswith("__"):
            raise AttributeError(name)
        return _Placeholder()


def install_stub(monkeypatch: Any, name: str, **attrs: Any) -> types.ModuleType:
    """Install (or extend) a permissive fake module at sys.modules[name].

    Reverted automatically by `monkeypatch` teardown.
    """
    module = sys.modules.get(name)
    if module is None:
        module = _PermissiveModule(name)
        monkeypatch.setitem(sys.modules, name, module)
    for attr, value in attrs.items():
        setattr(module, attr, value)
    return module


@pytest.fixture(scope="session", autouse=True)
def _rendered_litellm_state() -> None:
    """A fresh checkout has no operator `.env`, so the gitignored
    `runtime/state/litellm.yaml` that catalog tests read doesn't exist yet.
    Render it once from `.env.nexgate.example` with placeholders swapped for
    dummy non-placeholder values and subscription routes force-enabled, so
    every model is present. Never touch a real operator render if one
    already exists.
    """
    output = ROOT / "runtime/state/litellm.yaml"
    if output.exists():
        return

    fixture_env = ROOT / ".env.ci-test-fixture"
    lines = []
    for line in (ROOT / ".env.nexgate.example").read_text().splitlines():
        key, sep, value = line.partition("=")
        if sep and "replace-with-" in value:
            value = f"ci-test-{key.lower()}"
        elif sep and key == "NEXGATE_ENABLE_SUBSCRIPTION_ROUTES":
            value = "true"
        lines.append(f"{key}{sep}{value}")
    fixture_env.write_text("\n".join(lines) + "\n")

    spec = importlib.util.spec_from_file_location(
        "render_litellm_config_for_tests", ROOT / "scripts/render-litellm-config.py"
    )
    render_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(render_module)

    previous = os.environ.get("NEXGATE_ENV_FILE")
    os.environ["NEXGATE_ENV_FILE"] = fixture_env.name
    try:
        assert render_module.main() == 0, "render-litellm-config.py failed for the test fixture env"
    finally:
        if previous is None:
            os.environ.pop("NEXGATE_ENV_FILE", None)
        else:
            os.environ["NEXGATE_ENV_FILE"] = previous
        fixture_env.unlink(missing_ok=True)
