"""Bounded public-web capture with address pinning at the HTTP transport boundary."""

import asyncio
import hashlib
import ipaddress
import re
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx


class SourcePolicyViolation(ValueError):
    pass


@dataclass(frozen=True)
class CaptureEnvelope:
    url: str
    final_url: str
    redirect_chain: tuple[str, ...]
    pinned_addresses: tuple[str, ...]
    content_bytes: bytes
    content_sha256: str
    extracted_text: str
    extracted_text_sha256: str
    exact_passage: str
    retrieved_at: datetime
    extractor_version: str = 'html-text-v1'


class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'style', 'template'}:
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in {'script', 'style', 'template'}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden and data.strip():
            self.parts.append(data.strip())


def public_address(value):
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise SourcePolicyViolation('invalid resolved address') from exc
    if not address.is_global or address.is_multicast or address.is_unspecified or getattr(address, 'ipv4_mapped', None):
        raise SourcePolicyViolation('non-global address')
    if isinstance(address, ipaddress.IPv6Address) and (
        address.sixtofour is not None or address.teredo is not None
        or address in ipaddress.ip_network('64:ff9b::/96')
        or address in ipaddress.ip_network('64:ff9b:1::/48')
    ):
        raise SourcePolicyViolation('translated address unsupported')
    return str(address)


def validate_url(url):
    if not isinstance(url, str) or len(url) > 2048 or re.search(r'[\x00-\x20\\]', url):
        raise SourcePolicyViolation('invalid URL')
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
        if parts.scheme != 'https' or not host or parts.username is not None or parts.password is not None or port not in (None, 443):
            raise SourcePolicyViolation('only credential-free HTTPS on port 443 is allowed')
        host = host.encode('idna').decode('ascii').lower().rstrip('.')
        try:
            public_address(host)
        except SourcePolicyViolation:
            # Reject legacy integer/octal/hex IPv4 forms before DNS resolution.
            if ':' in host or re.fullmatch(r'[0-9.]+', host) or re.fullmatch(r'(?:0x[0-9a-f]+|[0-9]+)(?:\.(?:0x[0-9a-f]+|[0-9]+))*', host):
                raise
            if not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', host):
                raise SourcePolicyViolation('invalid hostname')
        return parts, host
    except (ValueError, UnicodeError) as exc:
        raise SourcePolicyViolation('invalid URL target') from exc


async def resolve_public(host, port):
    rows = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return tuple(dict.fromkeys(row[4][0] for row in rows))


class SourceClient:
    """Resolver/transport injection is trusted host configuration, never model input."""

    def __init__(self, *, resolver=resolve_public, transport=None):
        self.resolver = resolver
        self.transport = transport

    async def capture(self, url, policy):
        original = url
        redirects, pins = [], []
        timeout = httpx.Timeout(policy.read_timeout_seconds, connect=policy.connect_timeout_seconds)
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=timeout,
                limits=httpx.Limits(max_keepalive_connections=0), transport=self.transport) as client:
            for hop in range(policy.max_redirects + 1):
                parts, host = validate_url(url)
                addresses = tuple(public_address(item) for item in await self.resolver(host, 443))
                if not addresses:
                    raise SourcePolicyViolation('empty DNS answer')
                pinned = addresses[0]
                pins.append(pinned)
                target = '[' + pinned + ']' if ':' in pinned else pinned
                pinned_url = urlunsplit(('https', target, parts.path or '/', parts.query, ''))
                # HTTPX connects only to the validated IP. TLS still verifies the original host.
                async with client.stream('GET', pinned_url, headers={'Host': host, 'Accept-Encoding': 'identity'},
                                         extensions={'sni_hostname': host}) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        if hop == policy.max_redirects or 'location' not in response.headers:
                            raise SourcePolicyViolation('redirect budget exhausted')
                        url = urljoin(url, response.headers['location'])
                        validate_url(url)
                        redirects.append(url)
                        continue
                    if response.status_code != 200:
                        raise SourcePolicyViolation('source HTTP failure')
                    mime = response.headers.get('content-type', '').split(';')[0].strip().lower()
                    if mime not in {'text/html', 'text/plain', 'application/xhtml+xml'}:
                        raise SourcePolicyViolation('unsupported source MIME')
                    # Identity-only avoids decompression bombs and bounds wire and decoded bytes equally.
                    if response.headers.get('content-encoding', 'identity').lower() != 'identity':
                        raise SourcePolicyViolation('compressed source unsupported')
                    chunks, size = [], 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > policy.max_source_bytes:
                            raise SourcePolicyViolation('source byte ceiling exceeded')
                        chunks.append(chunk)
                    content = b''.join(chunks)
                    text = content.decode('utf-8', errors='replace')
                    if mime != 'text/plain':
                        parser = TextExtractor()
                        parser.feed(text)
                        text = '\n'.join(parser.parts)
                    if not text.strip():
                        raise SourcePolicyViolation('empty source text')
                    return CaptureEnvelope(original, url, tuple(redirects), tuple(pins), content,
                        hashlib.sha256(content).hexdigest(), text, hashlib.sha256(text.encode()).hexdigest(),
                        text[:16000], datetime.now(timezone.utc))
        raise SourcePolicyViolation('source capture failed')


async def fetch_source(url, policy, client):
    try:
        async with asyncio.timeout(policy.source_timeout_seconds):
            return await client.capture(url, policy)
    except (TimeoutError, httpx.HTTPError, OSError) as exc:
        raise SourcePolicyViolation('source transport or deadline failure') from exc
