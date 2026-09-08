"""Shared stubbing helper for tests that load runtime/config modules standalone.

runtime/config/ccproxy_callback.py and sitecustomize.py import heavy
third-party packages (litellm, ccproxy) at module scope. Tests load them via
importlib in isolation and need throwaway stand-ins in sys.modules.
`install_stub` creates one, tolerant of arbitrary attribute access, and
relies on `monkeypatch` to revert it after the test so stubs never leak into
other test files.
"""

from __future__ import annotations

import sys
import types
from typing import Any


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
