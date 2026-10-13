"""Expired leases must settle without re-enqueueing submitted work."""
import asyncio
from uuid import uuid4

from sqlalchemy import DateTime, func, update

from research_agent.a2a_service import DurableA2AService
from research_agent.a2a_store import SQLAlchemyA2ATaskStore, metadata, tasks


def test_expire_leases_fails_expired_working_and_keeps_submitted(tmp_path):
    asyncio.run(_scenario(tmp_path))


async def _scenario(tmp_path):
    store = SQLAlchemyA2ATaskStore.from_url(f"sqlite+aiosqlite:///{tmp_path / 'sweep.sqlite3'}")
    async with store.engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    try:
        expired, _ = await store.reserve(uuid4(), {"request": {"claim_id": "c"}}, "c" * 64)
        submitted, _ = await store.reserve(uuid4(), {"request": {"claim_id": "d"}}, "d" * 64)
        stale = func.strftime("%Y-%m-%d %H:%M:%f", "now", "-10 minutes", type_=DateTime(timezone=True))
        async with store.engine.begin() as connection:
            result = await connection.execute(
                update(tasks).where(tasks.c.id == str(expired.id)).values(
                    state="working", run_id=str(uuid4()), lease_owner="w", lease_expires_at=stale)
            )
            assert result.rowcount == 1
        await store.expire_leases()
        expired_row = await store.get(expired.id)
        submitted_row = await store.get(submitted.id)
        assert expired_row.state == "failed"
        assert expired_row.error == "research worker lease expired; manual reconciliation required"
        assert expired_row.lease_owner is None
        assert submitted_row.state == "submitted"

        enqueued = []

        async def enqueue(task_id):
            enqueued.append(task_id)

        service = DurableA2AService(store, enqueue=enqueue)
        assert await service.recover_pending() == 1
        assert enqueued == [submitted.id]
        await store.expire_leases()
        assert (await store.get(submitted.id)).state == "submitted"
    finally:
        await store.dispose()
