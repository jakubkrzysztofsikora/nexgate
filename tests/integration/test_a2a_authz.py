"""Authorization checks against the actual pinned LiteLLM HTTP service."""

import pytest
import hashlib
import time

from .conftest import docker


@pytest.fixture(scope="module")
def scoped_keys(real_proxy):
    keys = {}
    for label, agents in (("allowed", [real_proxy.agent_id]), ("denied", ["unrelated-agent"])):
        status, result = real_proxy.request("/key/generate", body={
            "key_alias": "spike-" + label, "object_permission": {"agents": agents},
        })
        assert status == 200, {"status": status}
        keys[label] = result["key"]
    return keys


def test_agent_card_requires_authentication(real_proxy):
    status, _ = real_proxy.request(
        "/a2a/quick-research/.well-known/agent-card.json", key=None,
    )
    assert status in (401, 403)


def test_pinned_proxy_uses_current_agent_card_route(real_proxy):
    status, card = real_proxy.request("/a2a/quick-research/.well-known/agent-card.json")
    assert status == 200, card
    assert card["name"] == "quick-research"


def test_agent_permission_rejects_a_different_agents_key(real_proxy, scoped_keys):
    status, _ = real_proxy.request(
        "/a2a/quick-research/.well-known/agent-card.json", key=scoped_keys["denied"],
    )
    assert status == 403


def test_authenticated_message_and_task_exchange(real_proxy, scoped_keys):
    status, sent = real_proxy.request("/a2a/quick-research", key=scoped_keys["allowed"], body={
        "jsonrpc": "2.0", "id": "send-probe", "method": "message/send",
        "params": {"message": {
            "kind": "message", "messageId": "spike-message", "role": "user",
            "parts": [{"kind": "data", "data": {"query": "spike"}}],
        }},
    })
    assert status == 200, sent
    assert "error" not in sent, sent
    task = sent["result"]
    assert task["status"]["state"] == "completed"
    assert task["artifacts"][0]["parts"][0]["data"] == {"query": "spike"}
    status, received = real_proxy.request("/a2a/quick-research", key=scoped_keys["allowed"], body={
        "jsonrpc": "2.0", "id": "get-probe", "method": "tasks/get",
        "params": {"id": task["id"]},
    })
    assert status == 200, received
    assert received["result"]["id"] == task["id"]
    status, missing = real_proxy.request("/a2a/quick-research", key=scoped_keys["allowed"], body={
        "jsonrpc": "2.0", "id": "missing-probe", "method": "tasks/get",
        "params": {"id": "missing-task"},
    })
    assert status == 200, missing
    assert missing["error"]["code"] == -32001

    key_hash = hashlib.sha256(scoped_keys["allowed"].encode()).hexdigest()
    query = ('SELECT count(*) FROM "LiteLLM_SpendLogs" '
             f"WHERE agent_id = '{real_proxy.agent_id}' AND api_key = '{key_hash}'")
    for _ in range(45):
        count = docker("exec", real_proxy.db, "psql", "-U", "spike", "-d", "spike", "-Atc", query)
        if int(count) > 0:
            break
        time.sleep(1)
    assert int(count) > 0, "No spend row attributed to both the registered agent and its scoped caller"


def test_claude_streaming_and_codex_routes_keep_working(real_proxy):
    status, message = real_proxy.request("/v1/messages", body={
        "model": "spike-claude", "max_tokens": 100000,
        "messages": [{"role": "user", "content": "probe"}],
    })
    assert status == 200, message
    # Haiku's model-specific output limit is 32768; the 16384 default applies
    # to other provider paths, not all native Anthropic requests.
    assert int(message["content"][0]["text"]) == 32768
    status, stream = real_proxy.request("/v1/messages", body={
        "model": "spike-claude", "max_tokens": 16, "stream": True,
        "messages": [{"role": "user", "content": "probe"}],
        "tools": [{"name": "lookup", "description": "Lookup probe", "input_schema": {
            "type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"],
        }}],
    })
    assert status == 200, stream
    assert "text_delta" in stream and "spike" in stream and "message_stop" in stream
    assert "input_json_delta" in stream and "lookup" in stream and "tool_use" in stream
    status, response = real_proxy.request("/v1/responses", body={
        "model": "spike-codex", "input": "probe",
    })
    assert status == 200, response
    assert response["output"][0]["content"][0]["text"] == "spike"
