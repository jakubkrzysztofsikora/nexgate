"""Small no-network OpenAI-compatible fixture used by the public demo."""

from __future__ import annotations

import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    server_version = "ModelGateDemo/0.1"

    def log_message(self, _format: str, *_args: object) -> None:
        # Avoid logging bearer tokens or request bodies in the demo container.
        return

    def _send(self, status: HTTPStatus, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        return self.headers.get("Authorization") == f"Bearer {os.environ['DEMO_API_KEY']}"

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._send(HTTPStatus.OK, {"status": "ok"})
            return
        if self.path == "/v1/models" and self._authorized():
            self._send(HTTPStatus.OK, {"object": "list", "data": [{"id": "demo-echo", "object": "model"}]})
            return
        self._send(HTTPStatus.UNAUTHORIZED if self.path == "/v1/models" else HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/v1/chat/completions":
            self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        if not self._authorized():
            self._send(HTTPStatus.UNAUTHORIZED, {"error": "invalid demo API key"})
            return
        self._send(HTTPStatus.OK, {"id": "demo", "object": "chat.completion", "model": "demo-echo", "choices": [{"index": 0, "message": {"role": "assistant", "content": "Demo gateway is healthy."}, "finish_reason": "stop"}]})


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("DEMO_PORT", "4010"))), Handler).serve_forever()
