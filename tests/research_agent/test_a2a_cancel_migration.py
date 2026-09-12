import asyncio

import asyncpg

from test_a2a_postgres_fencing import postgres_url
from test_a2a_postgres_migration import MIGRATION, pytestmark


def test_upgrade_does_not_acknowledge_legacy_pending_cancel(postgres_url):
    async def scenario():
        connection = await asyncpg.connect(postgres_url.replace("postgresql+asyncpg://", "postgresql://"))
        try:
            await connection.execute("""
                ALTER TABLE research_a2a_tasks DROP COLUMN cancel_requested;
                INSERT INTO research_a2a_tasks
                    (id, message_id, submission, submission_commitment, state, lease_owner, created_at, updated_at)
                VALUES ('legacy', 'legacy', '{}', repeat('a', 64), 'canceled', 'unacknowledged-owner', now(), now());
            """)
            for _ in range(2):
                await connection.execute(MIGRATION.read_text())
            row = await connection.fetchrow("SELECT * FROM research_a2a_tasks WHERE id = 'legacy'")
            assert row["state"] == "failed"
            assert row["cancel_requested"] is False
            assert row["artifact"] is None
            assert "reconciliation" in row["error"]
        finally:
            await connection.close()

    asyncio.run(scenario())
