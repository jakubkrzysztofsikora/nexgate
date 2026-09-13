import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
from a2a.client import A2ACardResolver
from a2a.client.transports.jsonrpc import JsonRpcTransport
from a2a.types import a2a_pb2
from research_agent.a2a_service import (
    DurableA2AService,
    LustroResearchSubmission,
    create_app,
    run_worker,
    validate_runtime_configuration,
)
from research_agent.a2a_store import SQLAlchemyA2ATaskStore, SubmissionConflict, metadata, tasks
from research_agent.research import ResearchRun, RunManifest
import pytest
from fastapi.testclient import TestClient
from google.protobuf.json_format import ParseDict
from pydantic import ValidationError
from sqlalchemy import update
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
        assert enqueued == [first.id, first.id]

        calls = 0

        async def research(request, policy, seed_records, checkpoint):
            nonlocal calls
            calls += 1
            await checkpoint()
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
        assert enqueued == [first.id, first.id]
        await store.dispose()

    run(scenario())


def test_acknowledgement_loss_resend_returns_the_reserved_task(tmp_path):
    async def scenario():
        store, service, enqueued = await make_service(tmp_path)
        envelope, _ = submission()
        accepted = await service.submit(envelope.request.request_id, envelope)
        recovered = await service.submit(envelope.request.request_id, envelope)
        assert recovered == accepted
        assert enqueued == [accepted.id, accepted.id]
        await store.dispose()

    run(scenario())


def test_resend_requeues_a_durably_reserved_task_after_enqueue_failure(tmp_path):
    async def scenario():
        store = SQLAlchemyA2ATaskStore.from_url(
            f"sqlite+aiosqlite:///{tmp_path / 'tasks.sqlite3'}"
        )
        async with store.engine.begin() as connection:
            await connection.run_sync(metadata.create_all)
        enqueued = []
        attempts = 0

        async def enqueue(task_id):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("queue temporarily unavailable")
            enqueued.append(task_id)

        service = DurableA2AService(store, enqueue=enqueue)
        envelope, _ = submission()
        with pytest.raises(RuntimeError, match="queue temporarily unavailable"):
            await service.submit(envelope.request.request_id, envelope)
        accepted = await service.submit(envelope.request.request_id, envelope)
        assert accepted.state == "submitted"
        assert enqueued == [accepted.id]
        assert await store.run_count(accepted.id) == 0
        await store.dispose()

    run(scenario())


def test_worker_retries_transient_claim_failure_and_restores_health(tmp_path):
    async def scenario():
        store, service, _ = await make_service(tmp_path)
        envelope, result = submission()
        task = await service.submit(envelope.request.request_id, envelope)
        queue = asyncio.Queue()
        await queue.put(task.id)
        original_claim = store.claim
        attempts = 0

        async def fail_once(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("database temporarily unavailable")
            return await original_claim(*args, **kwargs)

        store.claim = fail_once

        async def research(*_args):
            return result

        worker = asyncio.create_task(run_worker(service, queue, research, retry_delay=0.1))
        try:
            async with asyncio.timeout(2):
                while service.worker_healthy:
                    await asyncio.sleep(0.01)
            with TestClient(create_app(service, bearer_token="fixture")) as client:
                assert client.get("/healthz").status_code == 503
            async with asyncio.timeout(2):
                while (await service.get(task.id)).state != "completed":
                    await asyncio.sleep(0.01)
            assert attempts == 2
            # Completion is persisted by execute() before the worker's health
            # flag is restored in the enclosing worker loop.
            async with asyncio.timeout(2):
                while not service.worker_healthy:
                    await asyncio.sleep(0.01)
            assert service.worker_healthy
            assert await store.run_count(task.id) == 1
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            await store.dispose()

    run(scenario())


def test_execute_propagates_cancellation_so_worker_shutdown_completes(tmp_path):
    async def scenario():
        store, service, _ = await make_service(tmp_path)
        envelope, _ = submission()
        task = await service.submit(envelope.request.request_id, envelope)
        research_started = asyncio.Event()

        async def research(*_args):
            research_started.set()
            await asyncio.sleep(60)

        execution = asyncio.create_task(service.execute(task.id, research))
        try:
            async with asyncio.timeout(2):
                await research_started.wait()
            execution.cancel()
            with pytest.raises(asyncio.CancelledError):
                await execution
            # Cancellation must leave the run unacknowledged (working) for
            # lease-expiry reconciliation, never failed and never completed.
            assert (await service.get(task.id)).state == "working"
        finally:
            if not execution.done():
                execution.cancel()
                await asyncio.gather(execution, return_exceptions=True)
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
        with pytest.raises(RuntimeError, match="lease"):
            await store.complete(
                task.id, "stale-owner", run_id, result.model_dump(mode="json")
            )
        stale = await store.get(task.id)
        assert stale.state == "working"
        assert stale.artifact is None
        await store.cancel(task.id)
        await store.dispose()

    run(scenario())


def test_same_owner_cannot_heartbeat_after_lease_expiry(tmp_path):
    async def scenario():
        store, service, _ = await make_service(tmp_path)
        envelope, _ = submission()
        task = await service.submit(envelope.request.request_id, envelope)
        run_id = await store.claim(task.id, "expired-owner", lease_seconds=60)
        assert run_id is not None
        async with store.engine.begin() as connection:
            await connection.execute(
                update(tasks).where(tasks.c.id == str(task.id)).values(
                    lease_expires_at=datetime.now(UTC) - timedelta(seconds=5)
                )
            )
        assert not await store.heartbeat(
            task.id, "expired-owner", run_id, lease_seconds=60
        )
        await store.dispose()

    run(scenario())


def test_same_owner_cannot_complete_after_lease_expiry(tmp_path):
    async def scenario():
        store, service, _ = await make_service(tmp_path)
        envelope, result = submission()
        task = await service.submit(envelope.request.request_id, envelope)
        run_id = await store.claim(task.id, "expired-owner", lease_seconds=60)
        assert run_id is not None
        async with store.engine.begin() as connection:
            await connection.execute(
                update(tasks).where(tasks.c.id == str(task.id)).values(
                    lease_expires_at=datetime.now(UTC) - timedelta(seconds=5)
                )
            )
        with pytest.raises(RuntimeError, match="lease"):
            await store.complete(
                task.id,
                "expired-owner",
                run_id,
                result.model_dump(mode="json"),
            )
        persisted = await store.get(task.id)
        assert persisted.state == "working"
        assert persisted.artifact is None
        await store.dispose()

    run(scenario())


def test_expired_owner_checkpoint_blocks_external_operation(tmp_path):
    async def scenario():
        store, service, _ = await make_service(tmp_path)
        service.lease_seconds = -5
        envelope, result = submission()
        task = await service.submit(envelope.request.request_id, envelope)
        external_steps = []

        async def research(_request, _policy, _seed_records, checkpoint):
            await checkpoint()
            external_steps.append("provider-call")
            return result

        observed = await service.execute(task.id, research)
        assert observed.state == "failed"
        assert external_steps == []
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


def test_local_cancel_remains_pending_until_cooperative_checkpoint(tmp_path):
    async def scenario():
        store, service, _ = await make_service(tmp_path)
        envelope, result = submission()
        task = await service.submit(envelope.request.request_id, envelope)
        started = asyncio.Event()
        stopped = asyncio.Event()
        release = asyncio.Event()

        async def research(*_args):
            started.set()
            try:
                await release.wait()
                await _args[3]()
            finally:
                stopped.set()
            return result

        execution = asyncio.create_task(service.execute(task.id, research))
        await started.wait()
        canceled = await service.cancel(task.id)
        assert canceled.state == "working"
        assert canceled.cancellation_pending
        assert not stopped.is_set()
        release.set()
        assert (await execution).state == "canceled"
        persisted = await service.get(task.id)
        assert persisted.state == "canceled"
        assert persisted.artifacts == []
        await store.dispose()

    run(scenario())


def test_cross_instance_cancel_stops_the_owning_execution(tmp_path):
    async def scenario():
        store, owner, _ = await make_service(tmp_path)
        remote = DurableA2AService(store, enqueue=lambda _task_id: asyncio.sleep(0))
        envelope, result = submission()
        task = await owner.submit(envelope.request.request_id, envelope)
        started = asyncio.Event()
        stopped = asyncio.Event()
        release = asyncio.Event()

        async def research(*_args):
            started.set()
            try:
                await release.wait()
                await _args[3]()
            finally:
                stopped.set()
            return result

        execution = asyncio.create_task(owner.execute(task.id, research))
        await started.wait()
        assert (await remote.cancel(task.id)).cancellation_pending
        release.set()
        try:
            await asyncio.wait_for(stopped.wait(), timeout=1.5)
        except TimeoutError:
            execution.cancel()
            await execution
            raise
        completed = await asyncio.wait_for(asyncio.shield(execution), timeout=0.5)
        assert completed.state == "canceled"
        assert stopped.is_set()
        assert (await owner.get(task.id)).artifacts == []
        await store.dispose()

    run(scenario())


def test_cancel_returned_during_first_tavily_call_blocks_remaining_queries(tmp_path, monkeypatch):
    from research_agent.capture import EvidenceStore
    from research_agent.research import QueryPlan, research_cluster
    from research_agent.search_tavily import TavilySearch
    from test_research import MemoryBackend
    from test_policy import digest

    async def scenario():
        store, owner, _ = await make_service(tmp_path)
        owner.lease_seconds = 0.15

        async def unavailable_heartbeat(*args):
            await asyncio.Event().wait()

        monkeypatch.setattr(owner, "_heartbeat", unavailable_heartbeat)
        remote = DurableA2AService(store, enqueue=lambda _: asyncio.sleep(0))
        envelope, result = submission()
        archive = EvidenceStore(MemoryBackend())
        key = await archive.put_content_addressed(b"seed")
        seed = envelope.seed_records[0].model_copy(update={
            "archive_ref": key, "content_sha256": key.rsplit("/", 1)[1],
        })
        authority = envelope.policy.evidence_authorities[0].model_copy(update={"record_sha256": digest(seed)})
        policy = envelope.policy.model_copy(update={"evidence_authorities": [authority]})
        envelope = envelope.model_copy(update={"seed_records": [seed], "policy": policy})
        archive.seed_records = (seed,)
        task = await owner.submit(envelope.request.request_id, envelope)
        started, release = asyncio.Event(), asyncio.Event()
        events = []

        async def provider(url, payload, key, policy):
            events.append(payload["query"])
            if payload["query"] == "first":
                started.set()
                await release.wait()
            return {"results": []}

        class Model:
            alias = "test-model"

            async def plan_queries(self, *args):
                return QueryPlan(primary="first", contrary="second", correction="third", ownership="fourth")

            async def synthesize(self, *args):
                return result.draft

        async def research(request, policy, seeds, checkpoint):
            return await research_cluster(request, policy, TavilySearch(api_key="fixture"), Model(), archive, checkpoint)

        monkeypatch.setattr("research_agent.search_tavily.post_json", provider)
        execution = asyncio.create_task(owner.execute(task.id, research))
        try:
            await asyncio.wait_for(started.wait(), 2)
            assert (await asyncio.wait_for(remote.cancel(task.id), 3)).cancellation_pending
            events.append("CANCEL_RETURNED")
            release.set()
            assert (await asyncio.wait_for(execution, 2)).state == "canceled"
            assert events == ["first", "CANCEL_RETURNED"]
            assert (await store.get(task.id)).artifact is None
        finally:
            release.set()
            await asyncio.gather(execution, return_exceptions=True)
            await store.dispose()

    run(scenario())


def test_cross_instance_cancel_terminal_only_after_checkpoint(tmp_path):
    async def scenario():
        store, owner, _ = await make_service(tmp_path)
        remote = DurableA2AService(store, enqueue=lambda _task_id: asyncio.sleep(0))
        envelope, result = submission()
        task = await owner.submit(envelope.request.request_id, envelope)
        operation_started = asyncio.Event()
        release_operation = asyncio.Event()
        external_steps = []

        async def research(*args):
            operation_started.set()
            await release_operation.wait()
            if len(args) == 4:
                await args[3]()
            external_steps.append("subsequent-external-step")
            return result

        execution = asyncio.create_task(owner.execute(task.id, research))
        await operation_started.wait()
        pending = await remote.cancel(task.id)
        assert pending.state == "working"
        assert pending.cancellation_pending
        assert not await store.cancellation_acknowledged(task.id)

        release_operation.set()
        completed = await asyncio.wait_for(execution, timeout=0.5)
        assert (await remote.cancel(task.id)).state == "canceled"
        assert completed.state == "canceled"
        assert external_steps == []
        await store.dispose()

    run(scenario())


def test_cross_instance_cancel_is_acknowledged_when_inflight_operation_fails(tmp_path):
    async def scenario():
        store, owner, _ = await make_service(tmp_path)
        remote = DurableA2AService(store, enqueue=lambda _task_id: asyncio.sleep(0))
        envelope, _ = submission()
        task = await owner.submit(envelope.request.request_id, envelope)
        operation_started = asyncio.Event()
        release_operation = asyncio.Event()

        async def research(*_args):
            operation_started.set()
            await release_operation.wait()
            raise RuntimeError("provider failed during cancellation")

        execution = asyncio.create_task(owner.execute(task.id, research))
        await operation_started.wait()
        assert (await remote.cancel(task.id)).cancellation_pending
        release_operation.set()

        observed = await asyncio.wait_for(execution, timeout=0.5)
        assert (await remote.get(task.id)).state == "canceled"
        assert observed.state == "canceled"
        await store.dispose()

    run(scenario())


def test_pinned_a2a_v1_sdk_card_send_get_and_cancel(tmp_path):
    async def scenario():
        store, service, enqueued = await make_service(tmp_path)
        envelope, _ = submission()
        app = create_app(service, bearer_token="lustro-test-token")
        headers = {
            "Authorization": "Bearer lustro-test-token",
            "A2A-Version": "1.0",
        }
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://research-agent",
            headers=headers,
        ) as client:
            card = await A2ACardResolver(client, "http://research-agent").get_agent_card()
            assert len(card.supported_interfaces) == 1
            interface = card.supported_interfaces[0]
            assert interface.protocol_binding == "JSONRPC"
            assert interface.protocol_version == "1.0"

            transport = JsonRpcTransport(client, card, interface.url)
            request = ParseDict(
                {
                    "message": {
                        "messageId": str(envelope.request.request_id),
                        "role": "ROLE_USER",
                        "parts": [{"data": envelope.model_dump(mode="json")}],
                    }
                },
                a2a_pb2.SendMessageRequest(),
            )
            sent = await transport.send_message(request)
            assert sent.task.status.state == a2a_pb2.TASK_STATE_SUBMITTED
            assert [str(task_id) for task_id in enqueued] == [sent.task.id]

            fetched = await transport.get_task(a2a_pb2.GetTaskRequest(id=sent.task.id))
            assert fetched.id == sent.task.id
            assert fetched.status.state == a2a_pb2.TASK_STATE_SUBMITTED

            canceled = await transport.cancel_task(
                a2a_pb2.CancelTaskRequest(id=sent.task.id)
            )
            assert canceled.status.state == a2a_pb2.TASK_STATE_CANCELED

            legacy = await client.post(
                "/",
                json={
                    "jsonrpc": "2.0",
                    "id": "legacy",
                    "method": "message/send",
                    "params": {},
                },
            )
            assert legacy.json()["error"]["code"] == -32601
        await store.dispose()

    run(scenario())


def test_private_v1_jsonrpc_requires_bearer_and_strict_lustro_data_part(tmp_path):
    async def scenario():
        store, service, enqueued = await make_service(tmp_path)
        envelope, _ = submission()
        app = create_app(service, bearer_token="lustro-test-token")
        with TestClient(app) as client:
            headers = {
                "Authorization": "Bearer lustro-test-token",
                "A2A-Version": "1.0",
            }
            request = {
                "jsonrpc": "2.0",
                "id": "send-1",
                "method": "SendMessage",
                "params": {
                    "message": {
                        "messageId": str(envelope.request.request_id),
                        "role": "ROLE_USER",
                        "parts": [{"data": envelope.model_dump(mode="json")}],
                    }
                },
            }
            assert client.post("/", json=request).status_code == 401
            unsupported = client.post(
                "/",
                headers={"Authorization": "Bearer lustro-test-token"},
                json=request,
            )
            assert unsupported.json()["error"]["code"] == -32009
            response = client.post(
                "/",
                headers=headers,
                json=request,
            )
            assert response.status_code == 200
            task = response.json()["result"]["task"]
            assert task["status"]["state"] == "TASK_STATE_SUBMITTED"
            assert [str(task_id) for task_id in enqueued] == [task["id"]]

            card = client.get(
                "/.well-known/agent-card.json",
                headers=headers,
            ).json()
            assert card["supportedInterfaces"][0]["protocolVersion"] == "1.0"
            assert card["securitySchemes"] == {
                "lustroBearer": {
                    "httpAuthSecurityScheme": {"scheme": "bearer"}
                }
            }
            assert card["securityRequirements"] == [
                {"schemes": {"lustroBearer": {"list": []}}}
            ]

            conflict_request = envelope.model_dump(mode="json")
            conflict_request["request"]["claim_hypothesis"] = "Changed after reservation."
            conflict = request | {"id": "conflict-1"}
            conflict["params"] = {
                "message": request["params"]["message"]
                | {"parts": [{"data": conflict_request}]}
            }
            conflict_response = client.post(
                "/",
                headers=headers,
                json=conflict,
            ).json()
            assert conflict_response["error"]["code"] == -32010

            malformed = request | {"id": "bad-1"}
            malformed["params"] = {
                "message": request["params"]["message"]
                | {"parts": [{"data": {"schema_version": 1}}]}
            }
            bad = client.post(
                "/",
                headers=headers,
                json=malformed,
            )
            assert bad.status_code == 200
            assert bad.json()["error"]["code"] == -32602
            assert [str(task_id) for task_id in enqueued] == [task["id"]]

            invalid_wire = request | {
                "id": "bad-wire",
                "params": {
                    "message": request["params"]["message"] | {"role": "user"}
                },
            }
            wire_response = client.post(
                "/",
                headers=headers,
                json=invalid_wire,
            )
            assert wire_response.status_code == 200
            assert wire_response.json()["error"]["code"] == -32602

            get_response = client.post(
                "/",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": "get-1",
                    "method": "GetTask",
                    "params": {"id": task["id"]},
                },
            )
            assert get_response.json()["result"]["id"] == task["id"]

            cancel_response = client.post(
                "/",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": "cancel-1",
                    "method": "CancelTask",
                    "params": {"id": task["id"]},
                },
            )
            assert cancel_response.json()["result"]["status"]["state"] == "TASK_STATE_CANCELED"
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
