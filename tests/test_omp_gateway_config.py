import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_catalog_routes_real_backend_protocols_and_preserves_role_choices(tmp_path):
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"data": [
        {"model_name": "claude-opus-5-5", "litellm_params": {"model": "anthropic/claude-opus-5-5"}, "model_info": {"mode": "chat", "max_input_tokens": 1000000, "max_output_tokens": 128000, "supports_vision": True, "supports_reasoning": True}},
        {"model_name": "gpt-6.1-sol", "litellm_params": {"model": "chatgpt/gpt-6.1-sol"}, "model_info": {"mode": "responses", "max_input_tokens": 1050000, "max_output_tokens": 24000}},
        {"model_name": "glm-5.3", "litellm_params": {"model": "openai/glm-5.3"}, "model_info": {"mode": "chat", "max_input_tokens": 1000000, "max_output_tokens": 16384}},
        {"model_name": "qwen38-27b", "litellm_params": {"model": "openai/qwen3.8-27b"}, "model_info": {}},
        {"model_name": "fallback-claude-opus-5-5", "litellm_params": {"model": "anthropic/claude-opus-5-5"}, "model_info": {"mode": "chat"}},
        {"model_name": "embed", "litellm_params": {"model": "openai/embed"}, "model_info": {"mode": "embedding"}},
        {"model_name": "llm-422-sink", "litellm_params": {"model": "openai/sink"}, "model_info": {}},
    ]}))
    agent = tmp_path / "agent"
    agent.mkdir()
    (agent / "config.yml").write_text("modelRoles:\n  default: litellm/glm-5.3:auto\n  advisor: litellm/claude-opus-5-5:medium\nquietStartup: true\n")
    (agent / "models.yml").write_text("providers:\n  other:\n    api: openai-completions\n")
    result = subprocess.run([sys.executable, str(ROOT / "scripts/configure-omp-gateway.py"),
        "--catalog", str(catalog), "--agent-dir", str(agent), "--base-url", "http://localhost:4000/v1"],
        capture_output=True, text=True, env={**os.environ, "NEXGATE_OMP_API_KEY": "test-secret"})
    assert result.returncode == 0, result.stderr
    config = yaml.safe_load((agent / "models.yml").read_text())
    assert "other" in config["providers"]
    provider = config["providers"]["nexgate"]
    # OMP defaults Anthropic custom models to OAuth request shaping only
    # when provider auth is unset; apiKey suppresses that behavior.
    assert "auth" not in provider
    models = {m["id"]: m for m in provider["models"]}
    assert set(models) == {"claude-opus-5-5", "gpt-6.1-sol", "glm-5.3", "qwen38-27b"}
    assert models["claude-opus-5-5"]["api"] == "anthropic-messages"
    assert models["gpt-6.1-sol"]["api"] == "openai-responses"
    assert models["glm-5.3"]["api"] == "openai-completions"
    assert models["qwen38-27b"]["api"] == "openai-completions"
    assert models["claude-opus-5-5"]["input"] == ["text", "image"]
    assert models["glm-5.3"]["maxTokens"] == 16384
    assert "test-secret" not in (agent / "models.yml").read_text()
    assert "test-secret" not in result.stdout + result.stderr
    assert (agent / ".env").stat().st_mode & 0o777 == 0o600
    assert (agent / "extensions/nexgate-history-guard.js").exists()
    settings = yaml.safe_load((agent / "config.yml").read_text())
    assert settings["modelRoles"]["default"] == "nexgate/glm-5.3:auto"
    assert settings["modelRoles"]["advisor"] == "nexgate/claude-opus-5-5:medium"
    assert settings["quietStartup"] is True
    assert list(agent.glob("models.yml.backup-*"))
