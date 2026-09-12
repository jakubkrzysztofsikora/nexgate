import asyncio
from uuid import uuid4

from research_agent.a2a_service import (
    DurableA2AService,
    LustroResearchSubmission,
    create_app,
    validate_runtime_configuration,
)
from research_agent.a2a_store import SQLAlchemyA2ATaskStore, SubmissionConflict, metadata
from research_agent.research import ResearchRun, RunManifest
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_policy import setup_case


def run(coro):
    return asyncio.run(coro)


def submission():
    request, draft, records, policy, _ = setup_case()
    envelope = LustroResearchSubmission(
        request=request,
        policy=policy,
        seed_records=list(records.values()),
    )
    result = ResearchRun(
        draft=draft,
        evidence_records=list(records.values()),
        manifest=RunManifest(
            iterations=1,
            search_queries=4,
            model_calls=2,
            source_count=1,
            model_alias="test-model",
            search_adapter="test-search",
        ),
    )
    return envelope, result


async def make_service(tmp_path):
    store = SQLAlchemyA2ATaskStore.from_url(
        f"sqlite+aiosqlite:///{tmp_path / 'tasks.sqlite3'}"
    )
    async with store.engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    enqueued = []

    async def enqueue(task_id):
        enqueued.append(task_id)

    return store, DurableA2AService(store, enqueue=enqueue), enqueued


def test_submission_envelope_is_strict_and_versioned():
    envelope, _ = submission()
    assert envelope.schema_version == 1
    payload = envelope.model_dump(mode="json")
    payload["raw_comments"] = ["must stay in Lustro"]
    with pytest.raises(ValidationError):
        LustroResearchSubmission.model_validate(payload)
    payload = envelope.model_dump(mode="json")
    payload["schema_version"] = 2
    with pytest.raises(ValidationError):
        LustroResearchSubmission.model_validate(payload)


def test_duplicate_message_id_returns_one_task_and_one_research_run(tmp_path):
    async def scenario():
        store, service, enqueued = await make_service(tmp_path)
        envelope, result = submission()
        first, second = await asyncio.gather(
            service.submit(envelope.request.request_id, envelope),
            service.submit(envelope.request.request_id, envelope),
        )
        assert second.id == first.id
        assert enqueued == [first.id]

        calls = 0

        async def research(request, policy, seed_records):
            nonlocal calls
            calls += 1
            assert request == envelope.request
            assert policy == envelope.policy
            assert seed_records == tuple(envelope.seed_records)
            return result

        completed = await service.execute(first.id, research)
        duplicate = await service.submit(envelope.request.request_id, envelope)
        assert duplicate.id == first.id
        assert duplicate.state == "completed"
        assert duplicate.artifacts == completed.artifacts
        assert calls == 1
        assert await store.run_count(first.id) == 1
        assert enqueued == [first.id]
        await store.dispose()

    run(scenario())


def test_acknowledgement_loss_resend_returns_the_reserved_task(tmp_path):
    async def scenario():
        store, service, enqueued = await make_service(tmp_path)
        envelope, _ = submission()
        accepted = await service.submit(envelope.request.request_id, envelope)
        recovered = await service.submit(envelope.request.request_id, envelope)
        assert recovered == accepted
        assert enqueued == [accepted.id]
        await store.dispose()

    run(scenario())


def test_duplicate_message_id_with_different_commitment_is_rejected(tmp_path):
    async def scenario():
        store, service, enqueued = await make_service(tmp_path)
        envelope, _ = submission()
        accepted = await service.submit(envelope.request.request_id, envelope)
        changed = envelope.model_copy(
            update={
                "request": envelope.request.model_copy(
                    update={"claim_hypothesis": "A different hypothesis."}
                )
            }
        )
        with pytest.raises(SubmissionConflict):
            await service.submit(envelope.request.request_id, changed)
        assert enqueued == [accepted.id]
        await store.dispose()

    run(scenario())


def test_active_working_lease_cannot_be_claimed_by_another_instance(tmp_path):
    async def scenario():
        store, first, _ = await make_service(tmp_path)
        second = DurableA2AService(store, enqueue=lambda _task_id: asyncio.sleep(0))
        envelope, result = submission()
        task = await first.submit(envelope.request.request_id, envelope)
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def research(*_args):
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return result

        first_run = asyncio.create_task(first.execute(task.id, research))
        await started.wait()
        observed = await second.execute(task.id, research)
        assert observed.state == "working"
        assert calls == 1
        release.set()
        assert (await first_run).state == "completed"
        assert await store.run_count(task.id) == 1
        await store.dispose()

    run(scenario())


def test_expired_working_run_fails_closed_instead_of_rerunning(tmp_path):
    async def scenario():
        store, service, _ = await make_service(tmp_path)
        envelope, _ = submission()
        task = await service.submit(envelope.request.request_id, envelope)
        run_id = await store.claim(task.id, "crashed-worker", lease_seconds=0)
        assert run_id is not None
        recovered = await service.recover_pending()
        assert recovered == 0
        failed = await service.get(task.id)
        assert failed.state == "failed"
        assert failed.error == "research worker lease expired; manual reconciliation required"
        assert await store.run_count(task.id) == 1
        await store.dispose()

    run(scenario())


def test_stale_owner_cannot_persist_an_artifact(tmp_path):
    async def scenario():
        store, service, _ = await make_service(tmp_path)
        envelope, result = submission()
        task = await service.submit(envelope.request.request_id, envelope)
        run_id = await store.claim(task.id, "active-owner", lease_seconds=60)
        stale = await store.complete(
            task.id, "stale-owner", run_id, result.model_dump(mode="json")
        )
        assert stale.state == "working"
        assert stale.artifact is None
        await store.cancel(task.id)
        await store.dispose()

    run(scenario())


def test_get_cancel_and_recovery_use_durable_state(tmp_path):
    async def scenario():
        store, service, enqueued = await make_service(tmp_path)
        envelope, result = submission()
        task = await service.submit(envelope.request.request_id, envelope)
        assert await service.get(task.id) == task

        recovered = []

        async def recover_enqueue(task_id):
            recovered.append(task_id)

        restarted = DurableA2AService(store, enqueue=recover_enqueue)
        assert await restarted.recover_pending() == 1
        assert recovered == [task.id]

        canceled = await restarted.cancel(task.id)
        assert canceled.state == "canceled"
        calls = 0

        async def research(*_args):
            nonlocal calls
            calls += 1
            return result

        assert await restarted.execute(task.id, research) == canceled
        assert calls == 0
        assert await store.run_count(task.id) == 0
        assert enqueued == [task.id]
        await store.dispose()

    run(scenario())


def test_completed_and_failed_tasks_are_terminal(tmp_path):
    async def scenario():
        store, service, _ = await make_service(tmp_path)
        envelope, result = submission()
        completed_task = await service.submit(envelope.request.request_id, envelope)

        async def succeed(*_args):
            return result

        completed = await service.execute(completed_task.id, succeed)
        assert completed.state == "completed"
        assert len(completed.artifacts) == 1
        assert await service.cancel(completed.id) == completed

        failed_envelope = envelope.model_copy(
            update={"request": envelope.request.model_copy(update={"request_id": uuid4()})}
        )
        failed_task = await service.submit(failed_envelope.request.request_id, failed_envelope)

        async def fail(*_args):
            raise RuntimeError("provider unavailable")

        failed = await service.execute(failed_task.id, fail)
        assert failed.state == "failed"
        assert failed.error == "research execution failed"
        assert await service.cancel(failed.id) == failed
        await store.dispose()

    run(scenario())


def test_cancel_signals_and_awaits_local_execution_without_artifact(tmp_path):
    async def scenario():
        store, service, _ = await make_service(tmp_path)
        envelope, result = submission()
        task = await service.submit(envelope.request.request_id, envelope)
        started = asyncio.Event()
        stopped = asyncio.Event()

        async def research(*_args):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
            return result

        execution = asyncio.create_task(service.execute(task.id, research))
        await started.wait()
        canceled = await service.cancel(task.id)
        assert canceled.state == "canceled"
        assert stopped.is_set()
        assert (await execution).state == "canceled"
        persisted = await service.get(task.id)
        assert persisted.state == "canceled"
        assert persisted.artifacts == []
        await store.dispose()

    run(scenario())


def test_private_jsonrpc_requires_bearer_and_strict_lustro_data_part(tmp_path):
    async def scenario():
        store, service, enqueued = await make_service(tmp_path)
        envelope, _ = submission()
        app = create_app(service, bearer_token="lustro-test-token")
        with TestClient(app) as client:
            request = {
                "jsonrpc": "2.0",
                "id": "send-1",
                "method": "message/send",
                "params": {
                    "message": {
                        "kind": "message",
                        "messageId": str(envelope.request.request_id),
                        "role": "user",
                        "parts": [{"kind": "data", "data": envelope.model_dump(mode="json")}],
                    }
                },
            }
            assert client.post("/", json=request).status_code == 401
            response = client.post(
                "/",
                headers={"Authorization": "Bearer lustro-test-token"},
                json=request,
            )
            assert response.status_code == 200
            task = response.json()["result"]
            assert task["status"]["state"] == "submitted"
            assert [str(task_id) for task_id in enqueued] == [task["id"]]

            card = client.get(
                "/.well-known/agent-card.json",
                headers={"Authorization": "Bearer lustro-test-token"},
            ).json()
            assert card["protocolVersion"] == "1.0"
            assert card["securitySchemes"] == {
                "lustroBearer": {"type": "http", "scheme": "bearer"}
            }
            assert card["security"] == [{"lustroBearer": []}]
            from a2a.compat.v0_3.types import AgentCard

            assert AgentCard.model_validate(card).protocol_version == "1.0"

            conflict_request = envelope.model_dump(mode="json")
            conflict_request["request"]["claim_hypothesis"] = "Changed after reservation."
            conflict = request | {"id": "conflict-1"}
            conflict["params"] = {
                "message": request["params"]["message"]
                | {"parts": [{"kind": "data", "data": conflict_request}]}
            }
            conflict_response = client.post(
                "/",
                headers={"Authorization": "Bearer lustro-test-token"},
                json=conflict,
            ).json()
            assert conflict_response["error"]["code"] == -32009

            malformed = request | {"id": "bad-1"}
            malformed["params"] = {
                "message": request["params"]["message"]
                | {"parts": [{"kind": "data", "data": {"schema_version": 1}}]}
            }
            bad = client.post(
                "/",
                headers={"Authorization": "Bearer lustro-test-token"},
                json=malformed,
            )
            assert bad.status_code == 200
            assert bad.json()["error"]["code"] == -32602
            assert [str(task_id) for task_id in enqueued] == [task["id"]]

            get_response = client.post(
                "/",
                headers={"Authorization": "Bearer lustro-test-token"},
                json={
                    "jsonrpc": "2.0",
                    "id": "get-1",
                    "method": "tasks/get",
                    "params": {"id": task["id"]},
                },
            )
            assert get_response.json()["result"]["id"] == task["id"]

            cancel_response = client.post(
                "/",
                headers={"Authorization": "Bearer lustro-test-token"},
                json={
                    "jsonrpc": "2.0",
                    "id": "cancel-1",
                    "method": "tasks/cancel",
                    "params": {"id": task["id"]},
                },
            )
            assert cancel_response.json()["result"]["status"]["state"] == "canceled"
        await store.dispose()

    run(scenario())


def test_runtime_configuration_fails_closed_before_startup():
    valid = {
        "A2A_DATABASE_URL": "postgresql+asyncpg://service:secret@db/nexgate",
        "LUSTRO_A2A_BEARER_TOKEN": "dedicated-inbound-token",
        "RESEARCH_ARCHIVE_BUCKET": "private-research",
        "RESEARCH_ARCHIVE_ENDPOINT_URL": "https://s3.internal.example",
        "LITELLM_API_KEY": "dedicated-model-key",
        "TAVILY_API_KEY": "dedicated-search-key",
    }
    validate_runtime_configuration(valid)
    for key in valid:
        broken = valid | {key: "replace-with-placeholder"}
        with pytest.raises(RuntimeError, match=key):
            validate_runtime_configuration(broken)
