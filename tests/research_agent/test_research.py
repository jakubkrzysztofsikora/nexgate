import asyncio
import hashlib
import io

import httpx
import pytest

from research_agent.sources import SourcePolicyViolation, SourceClient, fetch_source
from research_agent.capture import EvidenceStore, CaptureError, S3Backend
from research_agent.research import research_cluster, QueryPlan
from test_policy import setup_case, change


class MemoryBackend:
    def __init__(self):
        self.data = {}

    async def put(self, key, value):
        self.data[key] = value

    async def get(self, key, limit):
        return self.data[key]


def run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize('url', [
    'http://127.0.0.1/admin', 'https://169.254.169.254/', 'file:///etc/passwd',
    'https://example.com@10.0.0.1/', 'https://[::1]/', 'https://2130706433/',
    'https://100.64.0.1/', 'https://0177.0.0.1/', 'https://0x7f000001/',
    'https://[::ffff:127.0.0.1]/', 'https://example.org:444/',
    'https://[64:ff9b::7f00:1]/', 'https://[2002:7f00:1::]/',
])
def test_private_and_ambiguous_targets_blocked(url):
    _, _, _, policy, _ = setup_case()
    async def no_dns(host, port):
        raise AssertionError('unsafe literal must be rejected before DNS')
    with pytest.raises(SourcePolicyViolation):
        run(fetch_source(url, policy, SourceClient(resolver=no_dns)))


def client_for(handler, addresses=('93.184.216.34',)):
    async def resolve(host, port):
        return addresses
    return SourceClient(resolver=resolve, transport=httpx.MockTransport(handler))


def test_dns_pinning_preserves_tls_name_and_host():
    def handler(request):
        assert request.url.host == '93.184.216.34'
        assert request.headers['host'] == 'public.example'
        assert request.extensions['sni_hostname'] == 'public.example'
        return httpx.Response(200, headers={'content-type': 'text/plain'}, content=b'Exact passage.')
    _, _, _, policy, _ = setup_case()
    capture = run(fetch_source('https://public.example/', policy, client_for(handler)))
    assert capture.exact_passage == 'Exact passage.'
    assert capture.content_bytes == b'Exact passage.'


def test_redirect_and_mixed_dns_answers_fail_closed():
    _, _, _, policy, _ = setup_case()
    handler = lambda request: httpx.Response(302, headers={'location': 'https://127.0.0.1/'})
    with pytest.raises(SourcePolicyViolation):
        run(fetch_source('https://public.example/', policy, client_for(handler)))
    with pytest.raises(SourcePolicyViolation):
        run(fetch_source('https://public.example/', policy, client_for(handler, ('93.184.216.34', '10.0.0.1'))))


def test_rebinding_on_next_hop_and_redirect_budget():
    _, _, _, policy, _ = setup_case()
    answers = iter([('93.184.216.34',), ('127.0.0.1',)])
    async def resolve(host, port):
        return next(answers)
    handler = lambda request: httpx.Response(302, headers={'location': '/next'})
    client = SourceClient(resolver=resolve, transport=httpx.MockTransport(handler))
    with pytest.raises(SourcePolicyViolation):
        run(fetch_source('https://public.example/', policy, client))
    with pytest.raises(SourcePolicyViolation, match='redirect budget'):
        run(fetch_source('https://public.example/', change(policy, max_redirects=1), client_for(handler)))


def test_capture_deadline_covers_dns():
    _, _, _, policy, _ = setup_case()
    async def resolve(host, port):
        await asyncio.sleep(5)
        return ('93.184.216.34',)
    with pytest.raises(SourcePolicyViolation, match='deadline'):
        run(fetch_source('https://public.example/', change(policy, source_timeout_seconds=1), SourceClient(resolver=resolve)))


@pytest.mark.parametrize('headers,body', [
    ({'content-type': 'application/pdf'}, b'pdf'),
    ({'content-type': 'text/plain'}, b'x' * 101),
    ({'content-type': 'text/plain', 'content-encoding': 'gzip'}, b'bad'),
])
def test_capture_type_encoding_and_byte_limits(headers, body):
    _, _, _, policy, _ = setup_case()
    policy = change(policy, max_source_bytes=100)
    with pytest.raises(SourcePolicyViolation):
        run(fetch_source('https://public.example/', policy, client_for(lambda request: httpx.Response(200, headers=headers, content=body))))


def test_content_store_idempotency_and_corruption():
    async def scenario():
        backend = MemoryBackend()
        store = EvidenceStore(backend)
        first = await store.put_content_addressed(b'abc')
        assert first == 'editorial-research/sha256/ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad'
        assert first == await store.put_content_addressed(b'abc')
        backend.data[first] = b'corrupt'
        with pytest.raises(CaptureError):
            await store.verify(first, hashlib.sha256(b'abc').hexdigest())
        del backend.data[first]
        with pytest.raises(CaptureError):
            await store.verify(first, hashlib.sha256(b'abc').hexdigest())
    run(scenario())


def test_s3_backend_reads_back_bytes_and_closes_stream():
    class S3:
        data = {}
        def put_object(self, *, Bucket, Key, Body, ContentType):
            self.data[(Bucket, Key)] = Body
        def get_object(self, *, Bucket, Key):
            value = self.data[(Bucket, Key)]
            self.body = io.BytesIO(value)
            return {'Body': self.body, 'ContentLength': len(value)}
    async def scenario():
        s3 = S3()
        store = EvidenceStore(S3Backend(s3, 'private-evidence'))
        key = await store.put_content_addressed(b'abc')
        assert s3.body.closed
        assert await store.verify(key, hashlib.sha256(b'abc').hexdigest()) == b'abc'
        assert s3.body.closed
    run(scenario())


def test_host_run_and_retry_budget_and_final_archive_verification():
    async def scenario():
        request, draft, records, policy, _ = setup_case()
        backend = MemoryBackend()
        store = EvidenceStore(backend)
        archive = await store.put_content_addressed(b'seed')
        seed = change(records['src:1'], archive_ref=archive, content_sha256=archive.rsplit('/', 1)[1])
        store.seed_records = (seed,)
        class Search:
            name = 'fake-search'
            client = client_for(lambda req: httpx.Response(200, headers={'content-type': 'text/plain'}, content=b'new evidence'))
            async def search(self, queries, policy):
                return ['https://public.example/', 'https://public.example/']
        class Model:
            alias = 'fake-model'
            calls = 0
            async def plan_queries(self, request, evidence, policy):
                return QueryPlan(primary='primary', contrary='contrary', correction='correction', ownership='ownership')
            async def synthesize(self, request, evidence, iteration, policy):
                self.calls += 1
                return draft if self.calls > 1 else change(draft, claim_member_evidence_ids=['invented'])
        model = Model()
        result = await research_cluster(request, policy, Search(), model, store)
        assert result.manifest.iterations == 2
        assert len(result.evidence_records) == 2
        assert result.manifest.search_queries == 8
        assert all(row.evidence_id.startswith('src:') for row in result.evidence_records)
        with pytest.raises(ValueError):
            await research_cluster(request, change(policy, max_iterations=1), Search(), Model(), store)
        class DeletingModel(Model):
            async def synthesize(self, *args):
                backend.data.clear()
                return draft
        with pytest.raises(CaptureError):
            await research_cluster(request, policy, Search(), DeletingModel(), store)
    run(scenario())
