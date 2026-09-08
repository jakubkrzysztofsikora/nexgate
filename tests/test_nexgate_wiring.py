"""bin/nexgate install must wire Claude Code, Codex, AND OpenCode to the
gateway with catalog-valid model names, and must stay reversible."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
NEXGATE_BIN = ROOT / "bin/nexgate"


@pytest.fixture()
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    project = tmp_path / "project"
    for path in (home, repo / ".claude" if False else repo, project):
        path.mkdir(parents=True, exist_ok=True)
    (repo / ".env").write_text(
        "\n".join(
            [
                "LITELLM_HOST=127.0.0.1",
                "LITELLM_PORT=4999",
                "LITELLM_MASTER_KEY=sk-test-master-key",
                "CLAUDE_CODE_DEFAULT_MODEL=claude-fable-5-1",
            ]
        )
        + "\n"
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("NEXGATE_ROOT", str(repo))

    # The installer skips Codex/OpenCode wiring entirely when their CLI is
    # not on PATH (bin/nexgate: `command -v codex/opencode || return 0`).
    # Stub no-op executables so wiring is exercised on any machine, not only
    # ones with both CLIs installed.
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    for cli in ("codex", "opencode"):
        stub = fake_bin / cli
        stub.write_text("#!/usr/bin/env bash\nexit 0\n")
        stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")

    for leaked in (
        "LITELLM_HOST",
        "LITELLM_PORT",
        "LITELLM_MASTER_KEY",
        "LITELLM_SCHEME",
        "CODEX_DEFAULT_MODEL",
        "CODEX_GATEWAY_URL",
        "CLAUDE_CODE_DEFAULT_MODEL",
        "CLAUDE_CODE_OPUS_MODEL",
        "CLAUDE_CODE_SONNET_MODEL",
        "CLAUDE_CODE_HAIKU_MODEL",
    ):
        monkeypatch.delenv(leaked, raising=False)
    return home, repo, project


def run_installer(project: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", "import sys; sys.argv=['nexgate', *sys.argv[1:]]; exec(open(sys.argv.pop(0)).read())", str(NEXGATE_BIN), *args],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=60,
    )


def run_bash_installer(project: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(NEXGATE_BIN), *args],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=60,
    )


class TestClaudeCodeWiring:
    def test_settings_local_carries_gateway_and_models(self, sandbox) -> None:
        _home, _repo, project = sandbox
        result = run_bash_installer(project, "install")
        assert result.returncode == 0, result.stderr
        settings = json.loads((project / ".claude/settings.local.json").read_text())
        env = settings["env"]
        assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:4999"
        assert "Bearer sk-test-master-key" in env["ANTHROPIC_CUSTOM_HEADERS"]
        assert settings["model"] == "claude-fable-5-1"
        for key in ("ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL"):
            assert env[key], key


class TestCodexWiring:
    def test_codex_config_toml_gets_gateway_provider(self, sandbox) -> None:
        home, _repo, project = sandbox
        result = run_bash_installer(project, "install")
        assert result.returncode == 0, result.stderr
        config = (home / ".codex/config.toml").read_text()
        assert "nexgate" in config
        assert "http://127.0.0.1:4999/v1" in config

    def test_repeated_installs_keep_codex_config_parseable(self, sandbox) -> None:
        import tomllib

        home, _repo, project = sandbox
        pre = home / ".codex/config.toml"
        pre.parent.mkdir(parents=True, exist_ok=True)
        pre.write_text(
            '[model_providers.litellm]\nname = "litellm"\nbase_url = "http://old:4000/v1"\n\n# <<< sovereign codex wiring <<<\n\n[profiles.fast]\nmodel = "gpt-4"\n'
        )
        run_bash_installer(project, "install")
        result = run_bash_installer(project, "install")
        assert result.returncode == 0, result.stderr
        parsed = tomllib.loads(pre.read_text())
        providers = parsed.get("model_providers", {})
        assert list(providers.keys()).count("litellm") == 1
        assert providers["litellm"]["base_url"] == "http://127.0.0.1:4999/v1"
        assert parsed["profiles"]["fast"]["model"] == "gpt-4"
        assert "sovereign codex wiring" not in pre.read_text()


class TestOpenCodeWiring:
    def test_opencode_config_gets_nexgate_provider(self, sandbox) -> None:
        home, _repo, project = sandbox
        result = run_bash_installer(project, "install")
        assert result.returncode == 0, result.stderr
        config_path = home / ".config/opencode/opencode.json"
        assert config_path.exists(), "installer wrote no opencode config"
        config = json.loads(config_path.read_text())
        provider = config["provider"]["nexgate"]
        assert provider["options"]["baseURL"] == "http://127.0.0.1:4999/v1"
        assert provider["options"]["apiKey"] == "{env:LITELLM_MASTER_KEY}"
        assert config["model"].startswith("nexgate/")
        assert provider["models"], "no models exposed"

    def test_opencode_models_exist_in_catalog(self, sandbox) -> None:
        home, _repo, project = sandbox
        run_bash_installer(project, "install")
        config = json.loads((home / ".config/opencode/opencode.json").read_text())
        import yaml

        catalog = yaml.safe_load((ROOT / "runtime/state/litellm.yaml").read_text())
        names = {m["model_name"] for m in catalog["model_list"]}
        wired = set(config["provider"]["nexgate"]["models"])
        assert wired <= names, wired - names

    def test_existing_opencode_config_is_preserved(self, sandbox) -> None:
        home, _repo, project = sandbox
        cfg_dir = home / ".config/opencode"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "opencode.json").write_text(
            json.dumps({"$schema": "https://opencode.ai/config.json", "theme": "existing-theme"})
        )
        run_bash_installer(project, "install")
        config = json.loads((cfg_dir / "opencode.json").read_text())
        assert config["theme"] == "existing-theme"
        assert config["$schema"] == "https://opencode.ai/config.json"
        assert "nexgate" in config["provider"]

    def test_restore_removes_opencode_wiring(self, sandbox) -> None:
        home, _repo, project = sandbox
        run_bash_installer(project, "install")
        result = run_bash_installer(project, "restore")
        assert result.returncode == 0, result.stderr
        config_path = home / ".config/opencode/opencode.json"
        if config_path.exists():
            config = json.loads(config_path.read_text())
            assert "nexgate" not in config.get("provider", {})


class TestEnvFileResolution:
    def test_env_nexgate_fallback_when_no_dot_env(self, tmp_path, monkeypatch) -> None:
        home = tmp_path / "home"
        repo = tmp_path / "repo"
        project = tmp_path / "project"
        for path in (home, repo, project):
            path.mkdir(parents=True)
        (repo / ".env.nexgate").write_text("LITELLM_MASTER_KEY=sk-fallback-key\n")
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("NEXGATE_ROOT", str(repo))
        monkeypatch.setenv("LITELLM_HOST", "127.0.0.1")
        monkeypatch.setenv("LITELLM_PORT", "4999")
        for leaked in ("LITELLM_MASTER_KEY", "LITELLM_SCHEME", "CODEX_DEFAULT_MODEL"):
            monkeypatch.delenv(leaked, raising=False)
        result = run_bash_installer(project, "install")
        assert result.returncode == 0, result.stderr
        settings = json.loads((project / ".claude/settings.local.json").read_text())
        assert "Bearer sk-fallback-key" in settings["env"]["ANTHROPIC_CUSTOM_HEADERS"]
