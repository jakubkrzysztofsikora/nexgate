import asyncio
import io
import threading
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from a2a.client import A2ACardResolver
from a2a.client.transports.jsonrpc import JsonRpcTransport
from a2a.types import a2a_pb2
from sqlalchemy import update

from research_agent.a2a_service import DurableA2AService, create_app
from research_agent.a2a_store import tasks
from research_agent.capture import EvidenceStore, S3Backend
from test_a2a_service import make_service, submission


@pytest.mark.parametrize("expired", [False, True])
@pytest.mark.parametrize("local", [False, True])
def test_s3_inflight_cancel_requires_owner_acknowledgement(tmp_path, monkeypatch, expired, local):
    async def scenario():
        store, owner, _ = await make_service(tmp_path)
        remote = owner if local else DurableA2AService(store, enqueue=lambda _: asyncio.sleep(0))
        entered, release = threading.Event(), threading.Event()
        events = []

        class S3:
            def put_object(self, **kwargs):
                self.content = kwargs["Body"]
                entered.set()
                assert release.wait(5)
                events.append("put_finished")

            def get_object(self, **kwargs):
                events.append("verify_get")
                return {"Body": io.BytesIO(self.content), "ContentLength": len(self.content)}

        async def unavailable_heartbeat(*args):
            await asyncio.Event().wait()

        monkeypatch.setattr(owner, "_heartbeat", unavailable_heartbeat)
        envelope, result = submission()
        task = await owner.submit(envelope.request.request_id, envelope)
        archive = EvidenceStore(S3Backend(S3(), "fixture"))

        async def research(request, policy, seeds, checkpoint):
            await archive.put_content_addressed(b"fixture archive")
            await checkpoint()
            events.append("operation_after_checkpoint")
            return result

        execution = asyncio.create_task(owner.execute(task.id, research))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            if expired:
                async with store.engine.begin() as connection:
                    await connection.execute(update(tasks).where(tasks.c.id == str(task.id)).values(
                        lease_expires_at=datetime.now(UTC) - timedelta(seconds=1)))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(remote, bearer_token="fixture")),
                                        base_url="http://test", headers={"Authorization": "Bearer fixture", "A2A-Version": "1.0"}) as client:
                card = await A2ACardResolver(client, "http://test").get_agent_card()
                transport = JsonRpcTransport(client, card, "http://test/")
                response = await asyncio.wait_for(transport.cancel_task(a2a_pb2.CancelTaskRequest(id=str(task.id))), 0.5)
                assert response.status.state != a2a_pb2.TASK_STATE_CANCELED
                if not expired:
                    assert response.status.state == a2a_pb2.TASK_STATE_WORKING
                    assert "pending" in response.status.message.parts[0].text.lower()
                assert not await store.cancellation_acknowledged(task.id)
                assert (await remote.get(task.id)).state != "canceled"
                events.append("pending_response")
                release.set()
                outcome = await asyncio.wait_for(execution, 2)
                assert outcome.state == ("failed" if expired else "canceled")
                assert await store.cancellation_acknowledged(task.id) is (not expired)
                assert events == ["pending_response", "put_finished", "verify_get"]
                assert (await remote.get(task.id)).artifacts == []
        finally:
            release.set()
            await asyncio.gather(execution, return_exceptions=True)
            await store.dispose()

    asyncio.run(scenario())


def test_timeout_unwind_cannot_acknowledge_inflight_thread(tmp_path):
    async def scenario():
        store, owner, _ = await make_service(tmp_path)
        entered, release = threading.Event(), threading.Event()
        envelope, _ = submission()
        task = await owner.submit(envelope.request.request_id, envelope)

        async def research(*args):
            async with asyncio.timeout(0.15):
                entered.set()
                await asyncio.to_thread(release.wait, 3)

        execution = asyncio.create_task(owner.execute(task.id, research))
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            assert (await owner.cancel(task.id)).cancellation_pending
            outcome = await asyncio.wait_for(execution, 1)
            assert outcome.state != "canceled"
            assert not await store.cancellation_acknowledged(task.id)
        finally:
            release.set()
            await asyncio.gather(execution, return_exceptions=True)
            await store.dispose()

    asyncio.run(scenario())
