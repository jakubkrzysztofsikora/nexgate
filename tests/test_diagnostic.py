from __future__ import annotations

from gateway import diagnostic


def test_diagnostic_requires_explicit_confirmation(monkeypatch, capsys) -> None:
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GATEWAY_DIAGNOSTIC_CONFIRM", raising=False)
    assert diagnostic.main() == 2
    assert "confirm" in capsys.readouterr().err


def test_diagnostic_is_disabled_in_ci(monkeypatch, capsys) -> None:
    monkeypatch.setenv("CI", "true")
    monkeypatch.setenv("GATEWAY_DIAGNOSTIC_CONFIRM", "run")
    assert diagnostic.main() == 2
    assert "disabled in CI" in capsys.readouterr().err


def test_diagnostic_redacts_configuration_errors(monkeypatch, capsys) -> None:
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("GATEWAY_DIAGNOSTIC_CONFIRM", "run")
    monkeypatch.setenv("GATEWAY_PROVIDER", "openai-compatible")
    monkeypatch.setenv("OPENAI_COMPATIBLE_BASE_URL", "ftp://private.invalid")
    monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", "secret-value")
    monkeypatch.setenv("GATEWAY_DIAGNOSTIC_MODEL", "fixture")
    assert diagnostic.main() == 1
    output = capsys.readouterr().err
    assert "secret-value" not in output
    assert "private.invalid" not in output
