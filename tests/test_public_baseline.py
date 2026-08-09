"""Contract checks for the portable public baseline."""

from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_demo_compose_is_standalone_and_loopback_only() -> None:
    compose = yaml.safe_load((ROOT / "compose.demo.yaml").read_text())
    service = compose["services"]["demo-gateway"]
    assert "env_file" not in service
    assert all(str(port).startswith("127.0.0.1:") for port in service["ports"])
    mounts = service.get("volumes", [])
    assert not any("~" in str(mount) or ".credentials" in str(mount) for mount in mounts)
    assert "@sha256:" in service["image"]


def test_public_commands_do_not_load_the_private_environment() -> None:
    makefile = (ROOT / "Makefile").read_text()
    for target in ("doctor", "tools", "configure", "demo", "validate"):
        assert f"{target}:" in makefile
    assert "include .env" not in makefile
    assert "compose.demo.yaml" in (ROOT / "scripts" / "demo.sh").read_text()


def test_provider_catalog_is_disabled_by_default() -> None:
    catalog = yaml.safe_load((ROOT / "config" / "provider-catalog.yaml").read_text())
    assert catalog["providers"]
    assert all(not provider["enabled"] for provider in catalog["providers"].values())
