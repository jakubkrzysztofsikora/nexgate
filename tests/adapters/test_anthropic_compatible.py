from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from gateway.anthropic_compatible import AnthropicCompatibleAdapter, AnthropicCompatibleConfig
from gateway.openai_compatible import UpstreamError


class FakeAnthropic(BaseHTTPRequestHandler):
    requests: list[dict] = []

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_POST(self) -> None:  # noqa: N802
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.__class__.requests.append({"path": self.path, "key": self.headers.get("x-api-key"), "version": self.headers.get("anthropic-version"), "payload": payload})
        if payload.get("model") == "bad":
            self.send_response(HTTPStatus.BAD_REQUEST)
            self.end_headers()
            self.wfile.write(b'{"error":"reflected prompt"}')
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
            body = b'event: content_block_delta\ndata: {"delta":{"type":"input_json_delta"}}\n\nevent: message_stop\ndata: {}\n\n'
            self.send_response(HTTPStatus.OK)
            self.send_header("content-type", "text/event-stream")
        else:
            body = json.dumps({"content": [{"type": "tool_use", "name": "lookup", "input": {}}]}).encode()
            self.send_response(HTTPStatus.OK)
            self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def adapter() -> AnthropicCompatibleAdapter:
    FakeAnthropic.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeAnthropic)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield AnthropicCompatibleAdapter(AnthropicCompatibleConfig(f"http://127.0.0.1:{server.server_port}/v1", "fixture-key"))
    finally:
        server.shutdown(); thread.join(); server.server_close()


def test_forwards_tool_payload_and_anthropic_headers(adapter: AnthropicCompatibleAdapter) -> None:
    result = adapter.complete({"model": "fixture", "messages": [], "tools": [{"name": "lookup", "input_schema": {"type": "object"}}]})
    request = FakeAnthropic.requests[-1]
    assert request["path"] == "/v1/messages"
    assert request["key"] == "fixture-key"
    assert request["version"] == "2023-06-01"
    assert request["payload"]["tools"][0]["name"] == "lookup"
    assert result["content"][0]["type"] == "tool_use"


def test_preserves_anthropic_sse(adapter: AnthropicCompatibleAdapter) -> None:
    stream = b"".join(adapter.stream({"model": "fixture", "messages": [], "stream": True}))
    assert b"content_block_delta" in stream
    assert b"message_stop" in stream


def test_sanitizes_upstream_error(adapter: AnthropicCompatibleAdapter) -> None:
    with pytest.raises(UpstreamError) as error:
        adapter.complete({"model": "bad", "messages": []})
    assert str(error.value) == "upstream HTTP 400"


def test_rejects_redirect_without_following_it(adapter: AnthropicCompatibleAdapter) -> None:
    with pytest.raises(UpstreamError) as error:
        adapter.complete({"model": "redirect", "messages": []})
    assert error.value.status == 302


def test_rejects_oversized_response(adapter: AnthropicCompatibleAdapter) -> None:
    with pytest.raises(UpstreamError, match="size limit"):
        adapter.complete({"model": "oversized", "messages": []})


def test_accepts_origin_or_v1_api_root() -> None:
    assert AnthropicCompatibleConfig("https://example.test", "key").messages_url() == "https://example.test/v1/messages"
    assert AnthropicCompatibleConfig("https://example.test/v1", "key").messages_url() == "https://example.test/v1/messages"
