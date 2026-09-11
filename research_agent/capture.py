"""Private content-addressed archives; citations require read-after-write verification."""

import asyncio
import hashlib
import re
from urllib.parse import urlsplit

from .models import ResearchEvidenceRecord

PREFIX = 'editorial-research/sha256/'


class CaptureError(ValueError):
    pass


class S3Backend:
    """Wrap the host's configured private S3-compatible client and bucket."""

    def __init__(self, client, bucket):
        self.client, self.bucket = client, bucket

    async def put(self, key, value):
        await asyncio.to_thread(self.client.put_object, Bucket=self.bucket, Key=key, Body=value,
                                ContentType='application/octet-stream')

    async def get(self, key, limit):
        def read():
            response = self.client.get_object(Bucket=self.bucket, Key=key)
            body = response['Body']
            try:
                if response.get('ContentLength', 0) > limit:
                    raise CaptureError('archive exceeds byte ceiling')
                return body.read(limit + 1)
            finally:
                body.close()
        return await asyncio.to_thread(read)


class EvidenceStore:
    def __init__(self, backend, *, seed_records=(), max_bytes=4_000_000):
        self.backend = backend
        self.seed_records = tuple(seed_records)
        self.max_bytes = max_bytes

    async def put_content_addressed(self, content):
        if not content or len(content) > self.max_bytes:
            raise CaptureError('invalid archive size')
        digest = hashlib.sha256(content).hexdigest()
        key = PREFIX + digest
        try:
            await self.backend.put(key, content)
        except Exception as exc:
            raise CaptureError('archive write failed') from exc
        await self.verify(key, digest)
        return key

    async def verify(self, key, digest):
        if not re.fullmatch(r'[0-9a-f]{64}', digest) or key != PREFIX + digest:
            raise CaptureError('invalid archive address')
        try:
            content = await self.backend.get(key, self.max_bytes)
        except Exception as exc:
            raise CaptureError('archive unavailable') from exc
        if len(content) > self.max_bytes or hashlib.sha256(content).hexdigest() != digest:
            raise CaptureError('archive digest mismatch')
        return content


def host_record_from_capture(envelope, archive_ref):
    host = urlsplit(envelope.final_url).hostname
    identity = hashlib.sha256((envelope.final_url + '\n' + envelope.content_sha256 + '\n' + envelope.extracted_text_sha256).encode()).hexdigest()
    return ResearchEvidenceRecord(evidence_id='src:' + identity, url=envelope.url,
        final_url=envelope.final_url, redirect_chain=list(envelope.redirect_chain), title=host,
        publisher=host, author=None, published_at=None, updated_at=None,
        retrieved_at=envelope.retrieved_at, source_class='commentary',
        independence_group='unreviewed:all', exact_passage=envelope.exact_passage,
        passage_locator='extracted text characters 0:' + str(len(envelope.exact_passage)),
        content_sha256=envelope.content_sha256, extracted_text_sha256=envelope.extracted_text_sha256,
        extractor_version=envelope.extractor_version, archive_ref=archive_ref)
