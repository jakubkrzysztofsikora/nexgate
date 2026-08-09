"""Dependency-free adapter for an operator-selected Anthropic-compatible API."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from gateway.openai_compatible import MAX_RESPONSE_BYTES, UpstreamError, _NoRedirect


@dataclass(frozen=True)
class AnthropicCompatibleConfig:
    base_url: str
    api_key: str
    api_version: str = "2023-06-01"
    timeout_seconds: float = 30.0

    def messages_url(self) -> str:
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("base_url must be an absolute http(s) URL")
        if parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ValueError("base_url must not include credentials, query, or fragment")
        path = parsed.path.rstrip("/")
        if path not in {"", "/v1"}:
            raise ValueError("base_url must be an origin or an API root ending in /v1")
        return f"{parsed.scheme}://{parsed.netloc}{path or '/v1'}/messages"


class AnthropicCompatibleAdapter:
    def __init__(self, config: AnthropicCompatibleConfig) -> None:
        self._config = config

    def _open(self, payload: dict):
        request = Request(
            self._config.messages_url(),
            data=json.dumps(payload, separators=(",", ":")).encode(),
            headers={"x-api-key": self._config.api_key, "anthropic-version": self._config.api_version, "content-type": "application/json"},
            method="POST",
        )
        try:
            return build_opener(_NoRedirect(), ProxyHandler({})).open(request, timeout=self._config.timeout_seconds)
        except HTTPError as exc:
            exc.close()
            raise UpstreamError(exc.code, f"upstream HTTP {exc.code}") from exc
        except (TimeoutError, URLError) as exc:
            raise UpstreamError(None, "upstream connection failed") from exc

    def complete(self, payload: dict) -> dict:
        with self._open(payload) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                raise UpstreamError(response.status, "upstream response exceeds size limit")
            try:
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
