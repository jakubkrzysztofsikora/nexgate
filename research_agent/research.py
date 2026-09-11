"""Bounded research producer. Editorial authority and approval stay with the host."""

import asyncio
from typing import Annotated

from pydantic import Field, ValidationError

from .models import ClusterResearchDraft, ResearchEvidenceRecord, ShortText, StrictModel
from .policy import PolicyViolation, canonical_bytes, validate_draft, validate_request
from .sources import fetch_source
from .capture import host_record_from_capture


class QueryPlan(StrictModel):
    primary: ShortText
    contrary: ShortText
    correction: ShortText
    ownership: ShortText


class RunManifest(StrictModel):
    iterations: int
    search_queries: int
    model_calls: int
    source_count: int
    model_alias: ShortText
    search_adapter: ShortText


class ResearchRun(StrictModel):
    draft: ClusterResearchDraft
    evidence_records: Annotated[list[ResearchEvidenceRecord], Field(max_length=64)]
    manifest: RunManifest


async def research_cluster(request, policy, search, model, evidence_store):
    validate_request(request, policy)
    async with asyncio.timeout(policy.run_timeout_seconds):
        records = {}
        for row in evidence_store.seed_records:
            row = ResearchEvidenceRecord.model_validate(row.model_dump())
            if row.evidence_id in records:
                raise PolicyViolation('duplicate seed evidence')
            await evidence_store.verify(row.archive_ref, row.content_sha256)
            records[row.evidence_id] = row
        if len(records) > policy.max_sources:
            raise PolicyViolation('seed evidence exceeds source budget')
        if not set(request.authority_evidence_ids) <= records.keys():
            raise PolicyViolation('missing trusted authority seed records')
        seen = {str(url) for row in records.values() for url in (row.url, row.final_url)}
        queries_used = 0
        for iteration in range(1, policy.max_iterations + 1):
            plan = await model.plan_queries(request, tuple(records.values()), policy)
            plan = QueryPlan.model_validate(plan.model_dump() if isinstance(plan, QueryPlan) else plan)
            queries = list(plan.model_dump().values())[:policy.max_queries_per_iteration]
            hits = await search.search(queries, policy, remaining_sources=policy.max_sources - len(records),
                                       excluded_urls=frozenset(seen))
            queries_used += len(queries)
            if len(hits) > policy.max_sources:
                raise PolicyViolation('search result budget exceeded')
            for url in hits:
                if url in seen or len(records) >= policy.max_sources:
                    continue
                envelope = await fetch_source(url, policy, search.client)
                archive = await evidence_store.put_content_addressed(envelope.content_bytes)
                row = host_record_from_capture(envelope, archive)
                records[row.evidence_id] = row
                seen.update((url, envelope.final_url))
            try:
                candidate = await model.synthesize(request, tuple(records.values()), iteration, policy)
                candidate = ClusterResearchDraft.model_validate(candidate.model_dump() if isinstance(candidate, ClusterResearchDraft) else candidate)
                validate_draft(candidate, request, records, policy)
            except (PolicyViolation, ValidationError):
                if iteration == policy.max_iterations:
                    raise
                continue
            for row in records.values():
                await evidence_store.verify(row.archive_ref, row.content_sha256)
            result = ResearchRun(draft=candidate, evidence_records=list(records.values()),
                manifest=RunManifest(iterations=iteration, search_queries=queries_used, model_calls=2 * iteration,
                                     source_count=len(records), model_alias=model.alias, search_adapter=search.name))
            if len(canonical_bytes(result)) > policy.max_result_bytes:
                raise PolicyViolation('run exceeds result byte ceiling')
            return result
        raise PolicyViolation('iteration budget exhausted')
