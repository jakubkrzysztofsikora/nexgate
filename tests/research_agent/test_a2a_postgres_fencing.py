"""Real PostgreSQL regressions for expiry during row-lock waits."""

import asyncio
import subprocess
import time
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text

from research_agent.a2a_store import LeaseLost, SQLAlchemyA2ATaskStore, tasks
from test_a2a_postgres_migration import MIGRATION, docker, pytestmark


@pytest.fixture(scope="module")
def postgres_url():
    name = f"nexgate-a2a-fence-{uuid4().hex[:10]}"
    docker("run", "-d", "--rm", "--name", name,
           "-e", "POSTGRES_PASSWORD=test-password", "-e", "POSTGRES_DB=nexgate",
           "-p", "127.0.0.1::5432", "postgres:16-alpine")
    try:
        # TCP readiness excludes the image's temporary initialization server.
        for _ in range(80):
            ready = subprocess.run(
                ["docker", "exec", name, "pg_isready", "-h", "127.0.0.1", "-U", "postgres", "-d", "nexgate"],
                capture_output=True,
            )
            if ready.returncode == 0:
                break
            time.sleep(0.25)
        else:
            pytest.fail("disposable PostgreSQL did not become ready")
        docker("exec", "-i", name, "psql", "-v", "ON_ERROR_STOP=1", "-U", "postgres", "-d", "nexgate", input_text=MIGRATION.read_text())
        port = docker("port", name, "5432/tcp").rsplit(":", 1)[1]
        yield f"postgresql+asyncpg://postgres:test-password@127.0.0.1:{port}/nexgate"
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


@pytest.mark.parametrize("operation", ["heartbeat", "complete", "checkpoint"])
def test_expiry_during_row_lock_wait_fences_same_owner_and_run(postgres_url, operation):
    async def scenario():
        store = SQLAlchemyA2ATaskStore.from_url(postgres_url)
        pending = None
        try:
            task, _ = await store.reserve(uuid4(), {}, "a" * 64)
            run_id = await store.claim(task.id, "owner", lease_seconds=0.6)
            async with store.engine.begin() as locker:
                await locker.execute(select(tasks).where(tasks.c.id == str(task.id)).with_for_update())
                if operation == "heartbeat":
                    action = store.heartbeat(task.id, "owner", run_id, lease_seconds=60)
                elif operation == "complete":
                    action = store.complete(task.id, "owner", run_id, {"must_not_persist": True})
                else:
                    action = store.run_active(task.id, "owner", run_id)
                pending = asyncio.create_task(action)
                async with store.engine.connect() as observer:
                    waiting = False
                    async with asyncio.timeout(3):
                        while not pending.done():
                            waiting = await observer.scalar(text(
                                "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                                "WHERE datname = current_database() AND wait_event_type = 'Lock')"
                            ))
                            if waiting:
                                break
                            await asyncio.sleep(0.01)
                    assert waiting and not pending.done(), "operation must wait on the held task row"
                    async with asyncio.timeout(3):
                        while not await observer.scalar(select(
                            tasks.c.lease_expires_at < func.clock_timestamp()
                        ).where(tasks.c.id == str(task.id))):
                            await asyncio.sleep(0.02)
                # The locker holds the row until the database proves expiry.
            if operation == "complete":
                with pytest.raises(LeaseLost):
                    await asyncio.wait_for(pending, 2)
            else:
                assert await asyncio.wait_for(pending, 2) is False
            persisted = await store.get(task.id)
            assert persisted.state == "working"
            assert persisted.artifact is None
        finally:
            if pending is not None:
                await asyncio.gather(pending, return_exceptions=True)
            await store.dispose()

    asyncio.run(scenario())
