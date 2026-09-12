import asyncio
import os
from pathlib import Path
import subprocess
import time
from uuid import uuid4

import pytest

from research_agent.a2a_store import SQLAlchemyA2ATaskStore


ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "runtime/migrations/001_research_a2a_tasks.sql"

pytestmark = pytest.mark.skipif(
    os.environ.get("NEXGATE_RUN_A2A_POSTGRES") != "1",
    reason="disposable PostgreSQL migration test is opt-in",
)


def docker(*args, input_text=None):
    return subprocess.run(
        ["docker", *args],
        input=input_text,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


def test_postgres_migration_applies_reapplies_and_passes_preflight():
    name = f"nexgate-a2a-migration-{uuid4().hex[:10]}"
    docker(
        "run", "-d", "--rm", "--name", name,
        "-e", "POSTGRES_PASSWORD=test-password",
        "-e", "POSTGRES_DB=nexgate",
        "-p", "127.0.0.1::5432",
        "postgres:16-alpine",
    )
    try:
        for _ in range(60):
            result = subprocess.run(
                [
                    "docker", "exec", name, "psql", "-U", "postgres", "-d", "nexgate",
                    "-Atc", "SELECT 1",
                ],
                text=True,
                capture_output=True,
            )
            if result.returncode == 0 and result.stdout.strip() == "1":
                break
            time.sleep(0.25)
        else:
            pytest.fail("disposable PostgreSQL did not become ready")

        sql = MIGRATION.read_text()
        for _ in range(2):
            docker("exec", "-i", name, "psql", "-v", "ON_ERROR_STOP=1", "-U", "postgres", "-d", "nexgate", input_text=sql)
        constraints = docker(
            "exec", name, "psql", "-U", "postgres", "-d", "nexgate", "-Atc",
            "SELECT conname FROM pg_constraint WHERE conrelid = 'research_a2a_tasks'::regclass ORDER BY conname",
        ).splitlines()
        assert "research_a2a_tasks_message_id_key" in constraints
        assert "research_a2a_tasks_state_check" in constraints
        port = docker("port", name, "5432/tcp").rsplit(":", 1)[1]

        async def preflight():
            store = SQLAlchemyA2ATaskStore.from_url(
                f"postgresql+asyncpg://postgres:test-password@127.0.0.1:{port}/nexgate"
            )
            await store.assert_schema_ready()
            await store.dispose()

        asyncio.run(preflight())
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
