"""Opt-in real container gate; no simulated proxy can satisfy this check.

Run with NEXGATE_RUN_A2A_SPIKE=1. Image compatibility is a prerequisite to
the database, exchange, and rollback portion of the spike.
"""
from __future__ import annotations

import os
import time

import pytest

from .conftest import RealProxy, completed_response, docker


pytestmark = pytest.mark.skipif(
    os.environ.get("NEXGATE_RUN_A2A_SPIKE") != "1",
    reason="real Docker A2A spike requires NEXGATE_RUN_A2A_SPIKE=1",
)


def test_real_image_installs_exact_a2a_versions(pinned_image) -> None:
    # The fixture builds the current Dockerfile and checks versions in-container
    # before any HTTP test is allowed to use the image.
    assert pinned_image == "nexgate-litellm:a2a-test"


def test_rollback_preserves_database_and_existing_routes(real_proxy):
    assert isinstance(getattr(real_proxy, "marker_key", None), str), "retain the baseline virtual key for rollback authentication"
    before = docker("exec", real_proxy.db, "psql", "-U", "spike", "-d", "spike", "-Atc",
                    'SELECT request_id FROM "LiteLLM_SpendLogs" ORDER BY request_id')
    docker("stop", real_proxy.container)
    docker("start", real_proxy.baseline)
    old = RealProxy(real_proxy.baseline)
    status = 0
    for _ in range(120):
        if docker("inspect", "-f", "{{.State.Running}}", real_proxy.baseline) != "true":
            break
        status, _ = old.request("/health/liveliness", key=None)
        if status == 200:
            break
        time.sleep(1)
    else:
        pytest.fail("rollback proxy did not start:\n" + docker("logs", real_proxy.baseline))
    assert status == 200, docker("logs", real_proxy.baseline)
    after = docker("exec", real_proxy.db, "psql", "-U", "spike", "-d", "spike", "-Atc",
                   'SELECT request_id FROM "LiteLLM_SpendLogs" ORDER BY request_id')
    assert set(before.splitlines()) <= set(after.splitlines()), "rollback lost spend records"
    count = docker("exec", real_proxy.db, "psql", "-U", "spike", "-d", "spike", "-Atc",
        'SELECT count(*) FROM "LiteLLM_VerificationToken" WHERE key_alias = \'migration-marker\'')
    assert count == "1", "rollback lost the pre-upgrade key"
    status, message = old.request("/v1/messages", key=real_proxy.marker_key, body={
        "model": "claude-haiku-4-5-20251001", "max_tokens": 16,
        "messages": [{"role": "user", "content": "rollback probe"}],
    })
    assert status == 200, message
    status, response = old.request("/v1/responses", key=real_proxy.marker_key, body={"model": "chatgpt/gpt-5.6-terra", "input": "rollback probe"})
    assert status == 200, response
    assert completed_response(response)["output"][0]["content"][0]["text"] == "spike"
    status, bridged = old.request("/v1/messages", key=real_proxy.marker_key, body={
        "model": "chatgpt/gpt-5.6-terra", "max_tokens": 16,
        "messages": [{"role": "user", "content": "rollback bridge probe"}],
    })
    assert status == 200, bridged
    assert bridged["content"][0]["text"] == "spike"
