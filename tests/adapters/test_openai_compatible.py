from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from gateway.openai_compatible import OpenAICompatibleAdapter, OpenAICompatibleConfig, UpstreamError


class FakeUpstream(BaseHTTPRequestHandler):
    requests: list[dict] = []

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_POST(self) -> None:  # noqa: N802
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.__class__.requests.append({"path": self.path, "auth": self.headers.get("Authorization"), "payload": payload})
        if payload.get("model") == "rate-limited":
            self.send_response(HTTPStatus.TOO_MANY_REQUESTS)
            self.end_headers()
            self.wfile.write(b'{"error":"rate limited"}')
            return
        if payload.get("model") == "reflected":
            self.send_response(HTTPStatus.BAD_REQUEST)
            self.end_headers()
            self.wfile.write(b'{"error":"reflected request content"}')
            return
        if payload.get("model") == "redirect":
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", "http://127.0.0.1:9/credential-sink")
            self.end_headers()
            return
        if payload.get("model") == "oversized":
            body = b"x" * (2 * 1024 * 1024 + 1)
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if payload.get("stream"):
            body = b'data: {"choices":[{"delta":{"tool_calls":[{"function":{"name":"lookup"}}]}}]}\n\ndata: [DONE]\n\n'
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream")
        else:
            body = json.dumps({"choices": [{"message": {"tool_calls": [{"function": {"name": "lookup", "arguments": "{}"}}]}}]}).encode()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def adapter() -> OpenAICompatibleAdapter:
    FakeUpstream.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstream)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield OpenAICompatibleAdapter(OpenAICompatibleConfig(f"http://127.0.0.1:{server.server_port}", "fixture-key"))
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_forwards_tool_payload_and_operator_key(adapter: OpenAICompatibleAdapter) -> None:
    result = adapter.complete({"model": "fixture", "messages": [], "tools": [{"type": "function", "function": {"name": "lookup"}}]})
    request = FakeUpstream.requests[-1]
    assert request["path"] == "/v1/chat/completions"
    assert request["auth"] == "Bearer fixture-key"
    assert request["payload"]["tools"][0]["function"]["name"] == "lookup"
    assert result["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "lookup"


def test_preserves_streaming_tool_events(adapter: OpenAICompatibleAdapter) -> None:
    event_stream = b"".join(adapter.stream({"model": "fixture", "messages": [], "stream": True}))
    assert b"data: [DONE]" in event_stream
    assert b'"tool_calls"' in event_stream


def test_classifies_upstream_http_errors(adapter: OpenAICompatibleAdapter) -> None:
    with pytest.raises(UpstreamError, match="upstream HTTP 429") as error:
        adapter.complete({"model": "rate-limited", "messages": []})
    assert error.value.status == 429


def test_does_not_expose_upstream_error_body(adapter: OpenAICompatibleAdapter) -> None:
    with pytest.raises(UpstreamError) as error:
        adapter.complete({"model": "reflected", "messages": [{"content": "private prompt"}]})
    assert str(error.value) == "upstream HTTP 400"


def test_rejects_redirect_without_following_it(adapter: OpenAICompatibleAdapter) -> None:
    with pytest.raises(UpstreamError) as error:
        adapter.complete({"model": "redirect", "messages": []})
    assert error.value.status == 302


def test_rejects_oversized_response(adapter: OpenAICompatibleAdapter) -> None:
    with pytest.raises(UpstreamError, match="size limit"):
        adapter.complete({"model": "oversized", "messages": []})


def test_accepts_origin_or_v1_api_root() -> None:
    assert OpenAICompatibleConfig("https://example.test", "key").chat_completions_url() == "https://example.test/v1/chat/completions"
    assert OpenAICompatibleConfig("https://example.test/v1", "key").chat_completions_url() == "https://example.test/v1/chat/completions"
