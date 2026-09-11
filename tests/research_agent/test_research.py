import asyncio
import hashlib
import io
import json

import httpx
import pytest

from research_agent.sources import SourcePolicyViolation, SourceClient, fetch_source
from research_agent.capture import EvidenceStore, CaptureError, S3Backend
from research_agent.research import research_cluster, QueryPlan
from research_agent.model_litellm import LiteLLMModel
from research_agent.search_tavily import TavilySearch
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


@pytest.mark.parametrize('seed_hit', [False, True])
def test_categories_reach_capture_with_seed_capacity(monkeypatch, seed_hit):
    async def scenario():
        request, draft, records, policy, _ = setup_case()
        store = EvidenceStore(MemoryBackend())
        archive = await store.put_content_addressed(b'seed')
        seed = change(records['src:1'], archive_ref=archive, content_sha256=archive.rsplit('/', 1)[1])
        # Twelve trusted records leave four slots: one for each query category.
        store.seed_records = (seed, *(change(seed, evidence_id=f'src:seed{i}', url=f'https://seed.example/{i}') for i in range(11)))
        async def api(url, payload, key, policy):
            hits = [{'url': f'https://public.example/{payload["query"]}/{i}'} for i in range(16)]
            if seed_hit:
                hits[0] = {'url': str(seed.url)}
            return {'results': hits}
        monkeypatch.setattr('research_agent.search_tavily.post_json', api)
        captured = []
        def page(request):
            captured.append(request.url.path.split('/')[1])
            return httpx.Response(200, headers={'content-type': 'text/plain'}, content=request.url.path.encode())
        class Model:
            alias = 'fake'
            async def plan_queries(self, *args):
                return QueryPlan(primary='primary', contrary='contrary', correction='correction', ownership='ownership')
            async def synthesize(self, *args):
                return draft
        result = await research_cluster(request, policy, TavilySearch(api_key='fake', client=client_for(page)), Model(), store)
        assert captured == ['primary', 'contrary', 'correction', 'ownership']
        assert len(result.evidence_records) == 16
    run(scenario())


def test_plain_and_html_archives_have_reproducible_extractor_semantics():
    from research_agent.capture import host_record_from_capture
    from research_agent import sources
    _, _, _, policy, _ = setup_case()
    content = b'<p>Visible</p><script>hidden</script>'
    captures = [run(fetch_source('https://public.example/', policy, client_for(
        lambda request, mime=mime: httpx.Response(200, headers={'content-type': mime}, content=content))))
        for mime in ('text/plain', 'text/html')]
    records = [host_record_from_capture(row, 'editorial-research/sha256/' + row.content_sha256) for row in captures]
    assert records[0].extractor_version != records[1].extractor_version
    assert captures[0].extracted_text == '<p>Visible</p><script>hidden</script>'
    assert captures[1].extracted_text == 'Visible'
    for capture, record in zip(captures, records):
        restored = sources.extract_text(content, record.extractor_version)
        assert restored == capture.extracted_text
        assert hashlib.sha256(restored.encode()).hexdigest() == record.extracted_text_sha256
        assert restored[:16000] == record.exact_passage


def test_synthesis_gets_host_reviewed_claim_when_hypothesis_differs(monkeypatch):
    request, draft, records, policy, _ = setup_case()
    request = change(request, claim_hypothesis='The number is 999.')
    async def api(url, payload, key, bounds):
        system = [row['content'] for row in payload['messages'] if row['role'] == 'system']
        trusted = next((json.loads(row) for row in system if row.startswith('{')), {})
        claim = trusted.get('host_reviewed_context', {}).get('reviewed_claim_text')
        assert claim == policy.reviewed_claim_text
        result = change(draft, claim_text=claim)
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': result.model_dump_json()}}]}
    monkeypatch.setattr('research_agent.model_litellm.post_json', api)
    result = run(LiteLLMModel(api_key='fake').synthesize(request, tuple(records.values()), 1, policy))
    from research_agent.policy import validate_draft
    validate_draft(result, request, records, policy)


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
            async def search(self, queries, policy, **kwargs):
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
