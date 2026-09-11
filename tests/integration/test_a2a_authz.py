"""Authorization checks against the actual pinned LiteLLM HTTP service."""

import pytest
import hashlib
import time
import json
import yaml

from .conftest import completed_response, docker


def upstream_calls(real_proxy):
    output = docker("exec", real_proxy.container, "python", "-S", "-c",
        "import urllib.request; print(urllib.request.urlopen('http://agent:9000/_probe/calls').read().decode())")
    calls = json.loads(output).get("calls")
    assert isinstance(calls, list), "controlled upstream must expose actual request observations"
    return calls


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


def test_scoped_key_can_discover_agent(real_proxy, scoped_keys):
    status, card = real_proxy.request(
        "/a2a/quick-research/.well-known/agent-card.json", key=scoped_keys["allowed"],
    )
    assert status == 200, card
    assert card["name"] == "quick-research"


@pytest.mark.parametrize("method", ["message/send", "tasks/get"])
@pytest.mark.parametrize("credential", ["missing", "invalid", "wrong-agent"])
def test_jsonrpc_rejection_does_not_invoke_upstream(real_proxy, scoped_keys, method, credential):
    key = {"missing": None, "invalid": "sk-invalid-disposable", "wrong-agent": scoped_keys["denied"]}[credential]
    before = upstream_calls(real_proxy)
    params = ({"message": {"kind": "message", "messageId": "rejected", "role": "user",
               "parts": [{"kind": "text", "text": "must not reach upstream"}]}}
              if method == "message/send" else {"id": "spike-task"})
    status, _ = real_proxy.request("/a2a/quick-research", key=key, body={
        "jsonrpc": "2.0", "id": "rejected", "method": method, "params": params,
    })
    assert status in (401, 403)
    assert upstream_calls(real_proxy) == before


def test_proxy_uses_rendered_cache_and_callback_settings(real_proxy):
    mounted = docker("exec", real_proxy.container, "python", "-S", "-c",
        "print(open('/app/config.yaml').read())")
    assert mounted == real_proxy.rendered_config.read_text().strip()
    config = yaml.safe_load(mounted)
    assert config["litellm_settings"].get("cache") is True
    assert config["litellm_settings"]["cache_params"]["type"] == "redis"
    assert config["litellm_settings"]["store_audit_logs"] is True
    assert config["general_settings"]["store_prompts_in_spend_logs"] is True
    assert {"prometheus", "claude_aware_compression.claude_aware_compression",
            "ccproxy_callback.ccproxy_handler"} <= set(config["litellm_settings"]["callbacks"])
    assert docker("exec", real_proxy.redis, "redis-cli", "PING") == "PONG"


def test_chatgpt_native_responses_and_anthropic_bridge(real_proxy):
    schema = {"type": "object", "properties": {"query": {"type": "string"}}}
    for route, body in [
        ("/v1/responses", {"model": "chatgpt/gpt-5.6-terra", "input": "native probe",
                          "tools": [{"type": "function", "name": "lookup", "parameters": schema}]}),
        ("/v1/messages", {"model": "chatgpt/gpt-5.6-terra", "max_tokens": 16,
                          "messages": [{"role": "user", "content": "bridge probe"}],
                          "tools": [{"name": "lookup", "input_schema": schema}]}),
    ]:
        before = len(upstream_calls(real_proxy))
        status, response = real_proxy.request(route, body=body)
        assert status == 200, response
        calls = upstream_calls(real_proxy)[before:]
        assert len(calls) == 1, calls
        assert calls[0]["path"].endswith("/responses"), calls
        assert calls[0]["model"] == "gpt-5.6-terra"
        assert calls[0]["tools"][0]["name"] == "lookup"
        assert calls[0]["tools"][0]["type"] == "function"
        assert "function" not in calls[0]["tools"][0]
        if route == "/v1/responses":
            assert completed_response(response)["output"][0]["content"][0]["text"] == "spike"
        else:
            assert response["content"][0]["text"] == "spike"


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
        "model": "claude-haiku-4-5-20251001", "max_tokens": 100000,
        "messages": [{"role": "user", "content": "probe"}],
    })
    assert status == 200, message
    # Haiku's model-specific output limit is 32768; the 16384 default applies
    # to other provider paths, not all native Anthropic requests.
    assert int(message["content"][0]["text"]) == 32768
    status, stream = real_proxy.request("/v1/messages", body={
        "model": "claude-haiku-4-5-20251001", "max_tokens": 16, "stream": True,
        "messages": [{"role": "user", "content": "probe"}],
        "tools": [{"name": "lookup", "description": "Lookup probe", "input_schema": {
            "type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"],
        }}],
    })
    assert status == 200, stream
    assert "text_delta" in stream and "spike" in stream and "message_stop" in stream
    assert "input_json_delta" in stream and "lookup" in stream and "tool_use" in stream
    status, response = real_proxy.request("/v1/responses", body={
        "model": "chatgpt/gpt-5.6-terra", "input": "probe",
    })
    assert status == 200, response
    assert completed_response(response)["output"][0]["content"][0]["text"] == "spike"
