from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_litellm_image_pins_a2a_capable_versions() -> None:
    dockerfile = (ROOT / "runtime/Dockerfile.litellm").read_text()
    assert "FROM python:3.13-slim-bookworm@sha256:ed86c82274b3c69b52fb5820f358f0bd7df0b603332063cb5c6e32bd220c3e6e" in dockerfile
    assert "ARG LITELLM_VERSION=1.100.1" in dockerfile
    assert '"a2a-sdk==1.1.2"' in dockerfile


def test_litellm_image_keeps_existing_compatibility_modules() -> None:
    compose = (ROOT / "compose.nexgate.yaml").read_text()
    for name in ("sitecustomize.py", "ccproxy_callback.py", "claude_aware_compression.py"):
        assert name in compose


def test_portable_litellm_catalog_preserves_the_migration_surface() -> None:
    catalog_text = (ROOT / "runtime/config/litellm.yaml.tmpl").read_text()
    catalog = yaml.safe_load(catalog_text)
    assert len(catalog["model_list"]) == 130
    assert all("model_name" in model and "litellm_params" in model for model in catalog["model_list"])
    # Every alias, including the free-GPU tier, must wire its endpoint through
    # the operator's ignored .env: the public catalog ships no endpoints.
    assert all(
        not str(model["litellm_params"].get("api_base", "")).startswith(("http://", "https://"))
        for model in catalog["model_list"]
    )
    assert catalog["general_settings"]["database_url"] == "os.environ/DATABASE_URL"


def test_runtime_compose_is_loopback_bound_and_has_optional_observability() -> None:
    compose = yaml.safe_load((ROOT / "compose.nexgate.yaml").read_text())
    services = compose["services"]
    assert {"litellm", "db", "redis", "prometheus", "grafana"} <= services.keys()
    assert services["prometheus"]["profiles"] == ["observability"]
    assert services["grafana"]["profiles"] == ["observability"]
    # Loopback is the portable default. Publishing on an extra host address
    # (e.g. a tailnet IP) is explicit opt-in via NEXGATE_BIND_HOST, and that
    # knob defaults to loopback as well.
    assert services["litellm"]["ports"] == [
        "127.0.0.1:${NEXGATE_PORT:-4000}:4000",
        "${NEXGATE_BIND_HOST:-127.0.0.1}:${NEXGATE_PORT:-4000}:4000",
    ]
    for service in ("prometheus", "grafana"):
        assert all(str(port).startswith("127.0.0.1:") for port in services[service]["ports"])


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker not available")
def test_compose_bind_host_collapses_and_extends(tmp_path: Path) -> None:
    """`docker compose config` ground truth: with NEXGATE_BIND_HOST unset the
    duplicated loopback entry collapses to a single binding; setting it adds
    exactly one extra binding instead of replacing loopback."""
    (tmp_path / "empty.env").write_text(
        "POSTGRES_PASSWORD=ci\nRESEARCH_ARCHIVE_ENDPOINT_URL=https://ci.invalid\n"
    )
    base_env = {
        key: value
        for key, value in os.environ.items()
        if key not in ("NEXGATE_BIND_HOST", "NEXGATE_ENV_FILE", "LITELLM_HOST", "LITELLM_PORT", "LITELLM_SCHEME")
    }
    cmd = [
        "docker", "compose", "--env-file", str(tmp_path / "empty.env"),
        "-f", str(ROOT / "compose.nexgate.yaml"), "config",
    ]
    probe = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT, env=base_env)
    if probe.returncode != 0:
        pytest.skip(f"docker compose unavailable: {probe.stderr.strip()[:120]}")
    ports = yaml.safe_load(probe.stdout)["services"]["litellm"]["ports"]
    assert [port["host_ip"] for port in ports] == ["127.0.0.1"]
    probe = subprocess.run(
        cmd, capture_output=True, text=True, cwd=ROOT,
        env={**base_env, "NEXGATE_BIND_HOST": "10.0.0.5"},
    )
    assert probe.returncode == 0, probe.stderr
    ports = yaml.safe_load(probe.stdout)["services"]["litellm"]["ports"]
    assert [port["host_ip"] for port in ports] == ["127.0.0.1", "10.0.0.5"]


def test_private_research_service_is_opt_in_and_not_host_exposed() -> None:
    compose = yaml.safe_load((ROOT / "compose.nexgate.yaml").read_text())
    service = compose["services"]["research-agent"]
    assert service["profiles"] == ["research"]
    assert not service.get("ports") or all(
        "0.0.0.0" not in port for port in service["ports"]
    )
    # Reachability: bound to the tailnet host only, never a public interface.
    assert service["ports"] == [
        "${LUSTRO_A2A_BIND_HOST:-100.116.31.6}:${LUSTRO_A2A_PORT:-9105}:9000"
    ]
    assert service["environment"]["A2A_DB_HOST"] == "db"
    assert service["environment"]["A2A_DB_USER"]
    assert service["environment"]["RESEARCH_ARCHIVE_ENDPOINT_URL"]
    assert service["environment"]["LUSTRO_A2A_BEARER_TOKEN"]
    assert service["environment"]["TAVILY_API_KEY"]
    dockerfile = (ROOT / "runtime/Dockerfile.research-agent").read_text()
    assert "FROM python:3.13-slim-bookworm@sha256:ed86c82274b3c69b52fb5820f358f0bd7df0b603332063cb5c6e32bd220c3e6e" in dockerfile
    assert "uv sync --frozen --no-dev" in dockerfile
    assert "research_agent.a2a_service:app_from_env" in dockerfile
    assert "a2a-sdk==1.1.2" in (ROOT / "pyproject.toml").read_text()
    assert (ROOT / "runtime/migrations/001_research_a2a_tasks.sql").exists()


def test_harness_wiring_uses_portable_defaults() -> None:
    harness = (ROOT / "bin/nexgate").read_text()
    assert "127.0.0.1" in harness
    assert "tail5d39b4" not in harness
    assert "bin/nexgate restore" in (ROOT / "docs/HARNESS-INTEGRATION.md").read_text()


def test_subscription_routes_are_explicitly_disabled_in_the_example_overlay() -> None:
    example = (ROOT / ".env.nexgate.example").read_text()
    assert "NEXGATE_ENABLE_SUBSCRIPTION_ROUTES=false" in example
    assert "runtime/state/chatgpt" in (ROOT / "compose.nexgate.yaml").read_text()


def test_provider_matrix_documents_every_catalog_alias() -> None:
    """The matrix is the operator-facing contract for what each route costs to
    enable; a route missing from it is a route nobody knows how to turn on."""
    catalog = yaml.safe_load((ROOT / "runtime/config/litellm.yaml.tmpl").read_text())
    documented = set(
        re.findall(r"^\| `([^`]+)` \| ", (ROOT / "docs/PROVIDER-MATRIX.md").read_text(), re.M)
    )
    aliases = {model["model_name"] for model in catalog["model_list"]}
    assert aliases == documented, (
        f"undocumented: {sorted(aliases - documented)}; stale: {sorted(documented - aliases)}"
    )


def test_every_fallback_target_resolves_to_a_defined_model() -> None:
    """A fallback chain naming a model that does not exist is a dead hop: the
    router skips it at the moment the primary is already failing."""
    catalog = yaml.safe_load((ROOT / "runtime/config/litellm.yaml.tmpl").read_text())
    defined = {model["model_name"] for model in catalog["model_list"]}
    router = catalog.get("router_settings", {})

    targets: set[str] = set()
    for key in ("fallbacks", "context_window_fallbacks", "content_policy_fallbacks"):
        for entry in router.get(key) or []:
            for chain in entry.values():
                targets.update(chain or [])
    targets.update(router.get("default_fallbacks") or [])

    assert targets <= defined, f"undefined fallback targets: {sorted(targets - defined)}"
    aliases = router.get("model_group_alias") or {}
    assert set(aliases.values()) <= defined, (
        f"undefined alias targets: {sorted(set(aliases.values()) - defined)}"
    )


def test_local_bielik_hook_is_inert_until_the_operator_opts_in(tmp_path: Path) -> None:
    """An operator who never set NEXGATE_BIELIK_API_BASE must not have weights
    downloaded or a llama-server started on their behalf by `make up`."""
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    for tool in ("hf", "tmux", "llama-server"):
        stub = fakebin / tool
        stub.write_text(f'#!/usr/bin/env bash\necho "INVOKED {tool} $*"\nexit 0\n')
        stub.chmod(0o755)

    env_file = tmp_path / "env"
    env_file.write_text("NEXGATE_BIELIK_API_BASE=https://replace-with-your-endpoint\n")

    result = subprocess.run(
        ["bash", str(ROOT / "scripts/local-bielik.sh")],
        env={
            "PATH": f"{fakebin}:/usr/bin:/bin",
            "HOME": str(tmp_path),
            "NEXGATE_ENV_FILE": str(env_file),
            "NEXGATE_LOCAL_STATE_DIR": str(tmp_path / "state"),
            "NEXGATE_LOCAL_LOG_DIR": str(tmp_path / "logs"),
            "NEXGATE_CHATGPT_STATE_DIR": str(tmp_path / "chatgpt"),
        },
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "INVOKED" not in result.stdout
    assert "skipping local server" in result.stdout


def test_render_prunes_fallback_hops_for_unconfigured_models() -> None:
    """Only model_list is filtered by configuration, so chains written for the
    full catalog must be pruned to what this deployment actually rendered."""
    spec = importlib.util.spec_from_file_location(
        "render_for_prune_test", ROOT / "scripts/render-litellm-config.py"
    )
    render = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(render)

    catalog = {
        "router_settings": {
            "default_fallbacks": ["kept", "dropped"],
            "model_group_alias": {"alias": "kept"},
            "fallbacks": [
                {"kept": ["dropped", "kept", "alias"]},
                {"dropped": ["kept"]},
                {"*": ["dropped"]},
            ],
        }
    }
    render.prune_chains(catalog, {"kept"})
    router = catalog["router_settings"]

    assert router["default_fallbacks"] == ["kept"]
    assert router["fallbacks"] == [{"kept": ["kept", "alias"]}, {"*": []}]
