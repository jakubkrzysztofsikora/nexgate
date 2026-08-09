"""Dependency-free adapter for an operator-selected OpenAI-compatible API."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class _NoRedirect(HTTPRedirectHandler):
    """Never forward an operator credential to a redirect target."""

    def redirect_request(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
        return None


@dataclass(frozen=True)
class OpenAICompatibleConfig:
    base_url: str
    api_key: str
    timeout_seconds: float = 30.0

    def chat_completions_url(self) -> str:
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("base_url must be an absolute http(s) URL")
        if parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ValueError("base_url must not include credentials, query, or fragment")
        path = parsed.path.rstrip("/")
        if path not in {"", "/v1"}:
            raise ValueError("base_url must be an origin or an API root ending in /v1")
        api_root = path or "/v1"
        return f"{parsed.scheme}://{parsed.netloc}{api_root}/chat/completions"


class UpstreamError(RuntimeError):
    """An upstream failure whose status is safe for routing policy to inspect."""

    def __init__(self, status: int | None, detail: str) -> None:
        super().__init__(detail)
        self.status = status


class OpenAICompatibleAdapter:
    """Forward payloads without changing caller-provided tool or SSE content."""

    def __init__(self, config: OpenAICompatibleConfig) -> None:
        self._config = config

    def _request(self, payload: dict) -> Request:
        return Request(
            self._config.chat_completions_url(),
            data=json.dumps(payload, separators=(",", ":")).encode(),
            headers={
                "Authorization": f"Bearer {self._config.api_key}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream" if payload.get("stream") else "application/json",
            },
            method="POST",
        )

    def _open(self, payload: dict):
        try:
            return build_opener(_NoRedirect(), ProxyHandler({})).open(self._request(payload), timeout=self._config.timeout_seconds)
        except HTTPError as exc:
            # Provider error bodies can reflect prompts or internal diagnostics.
            exc.close()
            raise UpstreamError(exc.code, f"upstream HTTP {exc.code}") from exc
        except (TimeoutError, URLError) as exc:
            raise UpstreamError(None, "upstream connection failed") from exc

    def complete(self, payload: dict) -> dict:
        with self._open(payload) as response:
            try:
                body = response.read(MAX_RESPONSE_BYTES + 1)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise UpstreamError(response.status, "upstream response exceeds size limit")
                return json.loads(body)
            except json.JSONDecodeError as exc:
                raise UpstreamError(response.status, "upstream returned invalid JSON") from exc

    def stream(self, payload: dict) -> Iterator[bytes]:
        if not payload.get("stream"):
            raise ValueError("stream payload must set stream=true")
        response = self._open(payload)
        try:
            while chunk := response.read(4096):
                yield chunk
        finally:
            response.close()
