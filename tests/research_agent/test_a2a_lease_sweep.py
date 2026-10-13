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


def test_sweeper_survives_expire_failures():
    asyncio.run(_sweeper_scenario())


async def _sweeper_scenario():
    from research_agent.a2a_service import sweep_expired_leases

    class FlakyStore:
        def __init__(self):
            self.calls = 0

        async def expire_leases(self):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("db unavailable")

    store = FlakyStore()
    task = __import__("asyncio").create_task(sweep_expired_leases(store, interval_seconds=0.01))
    await __import__("asyncio").sleep(0.06)
    try:
        assert not task.done(), "sweeper died on a failed sweep"
        assert store.calls >= 2, "sweeper did not retry after failure"
    finally:
        task.cancel()
        await __import__("asyncio").gather(task, return_exceptions=True)
