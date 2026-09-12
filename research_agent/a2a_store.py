"""Persistent task ledger for the private Lustro research A2A boundary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import JSON, DateTime, Integer, MetaData, String, Table, Column, insert, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


metadata = MetaData()

tasks = Table(
    "research_a2a_tasks",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("message_id", String(36), nullable=False, unique=True),
    Column("submission", JSON, nullable=False),
    Column("submission_commitment", String(64), nullable=False),
    Column("state", String(16), nullable=False),
    Column("artifact", JSON),
    Column("error", String(500)),
    Column("run_count", Integer, nullable=False, default=0),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)


@dataclass(frozen=True)
class StoredTask:
    id: UUID
    message_id: UUID
    state: str
    artifact: dict[str, Any] | None
    error: str | None
    created_at: datetime
    updated_at: datetime


class TaskNotFound(LookupError):
    pass


def _stored(row: Any) -> StoredTask:
    return StoredTask(
        id=UUID(row.id),
        message_id=UUID(row.message_id),
        state=row.state,
        artifact=row.artifact,
        error=row.error,
        created_at=row.created_at.replace(tzinfo=row.created_at.tzinfo or UTC),
        updated_at=row.updated_at.replace(tzinfo=row.updated_at.tzinfo or UTC),
    )


class SQLAlchemyA2ATaskStore:
    """Async SQL store with a database-enforced unique message reservation."""

    def __init__(self, engine: AsyncEngine):
        self.engine = engine

    @classmethod
    def from_url(cls, url: str) -> "SQLAlchemyA2ATaskStore":
        return cls(create_async_engine(url, pool_pre_ping=True))

    async def create_schema(self) -> None:
        async with self.engine.begin() as connection:
            await connection.run_sync(metadata.create_all)

    async def dispose(self) -> None:
        await self.engine.dispose()

    async def reserve(
        self,
        message_id: UUID,
        submission: dict[str, Any],
        commitment: str,
    ) -> tuple[StoredTask, bool]:
        now = datetime.now(UTC)
        values = {
            "id": str(uuid4()),
            "message_id": str(message_id),
            "submission": submission,
            "submission_commitment": commitment,
            "state": "submitted",
            "artifact": None,
            "error": None,
            "run_count": 0,
            "created_at": now,
            "updated_at": now,
        }
        dialect = self.engine.dialect.name
        statement = insert(tasks).values(**values)
        if dialect == "postgresql":
            statement = postgresql_insert(tasks).values(**values).on_conflict_do_nothing(
                index_elements=[tasks.c.message_id]
            )
        elif dialect == "sqlite":
            statement = sqlite_insert(tasks).values(**values).on_conflict_do_nothing(
                index_elements=[tasks.c.message_id]
            )

        async with self.engine.begin() as connection:
            result = await connection.execute(statement)
            created = result.rowcount == 1

        async with self.engine.connect() as connection:
            row = (
                await connection.execute(
                    select(tasks).where(tasks.c.message_id == str(message_id))
                )
            ).one()
        return _stored(row), created

    async def get(self, task_id: UUID) -> StoredTask:
        async with self.engine.connect() as connection:
            row = (
                await connection.execute(select(tasks).where(tasks.c.id == str(task_id)))
            ).one_or_none()
        if row is None:
            raise TaskNotFound(str(task_id))
        return _stored(row)

    async def submission(self, task_id: UUID) -> dict[str, Any]:
        async with self.engine.connect() as connection:
            value = await connection.scalar(
                select(tasks.c.submission).where(tasks.c.id == str(task_id))
            )
        if value is None:
            raise TaskNotFound(str(task_id))
        return value

    async def claim(self, task_id: UUID) -> bool:
        now = datetime.now(UTC)
        async with self.engine.begin() as connection:
            result = await connection.execute(
                update(tasks)
                .where(tasks.c.id == str(task_id), tasks.c.state == "submitted")
                .values(
                    state="working",
                    run_count=tasks.c.run_count + 1,
                    updated_at=now,
                )
            )
        return result.rowcount == 1

    async def complete(self, task_id: UUID, artifact: dict[str, Any]) -> StoredTask:
        await self._terminal_update(task_id, "completed", artifact=artifact)
        return await self.get(task_id)

    async def fail(self, task_id: UUID, error: str) -> StoredTask:
        await self._terminal_update(task_id, "failed", error=error)
        return await self.get(task_id)

    async def _terminal_update(
        self,
        task_id: UUID,
        state: str,
        *,
        artifact: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        async with self.engine.begin() as connection:
            await connection.execute(
                update(tasks)
                .where(tasks.c.id == str(task_id), tasks.c.state == "working")
                .values(
                    state=state,
                    artifact=artifact,
                    error=error,
                    updated_at=datetime.now(UTC),
                )
            )

    async def cancel(self, task_id: UUID) -> StoredTask:
        async with self.engine.begin() as connection:
            await connection.execute(
                update(tasks)
                .where(
                    tasks.c.id == str(task_id),
                    tasks.c.state.in_(("submitted", "working")),
                )
                .values(state="canceled", updated_at=datetime.now(UTC))
            )
        return await self.get(task_id)

    async def recoverable(self) -> list[UUID]:
        """Reset interrupted workers and return every task that needs dispatch."""
        async with self.engine.begin() as connection:
            await connection.execute(
                update(tasks)
                .where(tasks.c.state == "working")
                .values(state="submitted", updated_at=datetime.now(UTC))
            )
            rows = (
                await connection.execute(
                    select(tasks.c.id)
                    .where(tasks.c.state == "submitted")
                    .order_by(tasks.c.created_at)
                )
            ).scalars().all()
        return [UUID(task_id) for task_id in rows]

    async def run_count(self, task_id: UUID) -> int:
        async with self.engine.connect() as connection:
            value = await connection.scalar(
                select(tasks.c.run_count).where(tasks.c.id == str(task_id))
            )
        if value is None:
            raise TaskNotFound(str(task_id))
        return value
