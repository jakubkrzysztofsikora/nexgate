"""Persistent task ledger for the private Lustro research A2A boundary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import JSON, Column, DateTime, Integer, MetaData, String, Table, inspect, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


metadata = MetaData()
tasks = Table(
    "research_a2a_tasks", metadata,
    Column("id", String(36), primary_key=True),
    Column("message_id", String(36), nullable=False, unique=True),
    Column("submission", JSON, nullable=False),
    Column("submission_commitment", String(64), nullable=False),
    Column("state", String(16), nullable=False),
    Column("artifact", JSON), Column("error", String(500)),
    Column("run_count", Integer, nullable=False, default=0),
    Column("run_id", String(36)), Column("lease_owner", String(200)),
    Column("lease_expires_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)


@dataclass(frozen=True)
class StoredTask:
    id: UUID
    message_id: UUID
    submission_commitment: str
    state: str
    artifact: dict[str, Any] | None
    error: str | None
    run_id: UUID | None
    lease_owner: str | None
    lease_expires_at: datetime | None
    created_at: datetime
    updated_at: datetime


class TaskNotFound(LookupError):
    pass


class SubmissionConflict(ValueError):
    """The idempotency key is already bound to different trusted inputs."""


def _aware(value: datetime | None) -> datetime | None:
    return None if value is None else value.replace(tzinfo=value.tzinfo or UTC)


def _stored(row: Any) -> StoredTask:
    return StoredTask(
        id=UUID(row.id), message_id=UUID(row.message_id),
        submission_commitment=row.submission_commitment, state=row.state,
        artifact=row.artifact, error=row.error,
        run_id=UUID(row.run_id) if row.run_id else None, lease_owner=row.lease_owner,
        lease_expires_at=_aware(row.lease_expires_at), created_at=_aware(row.created_at),
        updated_at=_aware(row.updated_at),
    )


class SQLAlchemyA2ATaskStore:
    """Async SQL store with unique reservation and owner-bound run leases."""

    def __init__(self, engine: AsyncEngine):
        self.engine = engine

    @classmethod
    def from_url(cls, url: str) -> "SQLAlchemyA2ATaskStore":
        return cls(create_async_engine(url, pool_pre_ping=True))

    async def assert_schema_ready(self) -> None:
        required = {column.name for column in tasks.columns}

        def check(connection):
            schema = inspect(connection)
            if not schema.has_table(tasks.name):
                raise RuntimeError("research A2A schema missing; apply migration 001")
            actual = {column["name"] for column in schema.get_columns(tasks.name)}
            if missing := required - actual:
                raise RuntimeError(f"research A2A schema missing columns: {sorted(missing)}")
            unique_sets = [set(row.get("column_names") or ()) for row in schema.get_unique_constraints(tasks.name)]
            unique_sets += [set(row.get("column_names") or ()) for row in schema.get_indexes(tasks.name) if row.get("unique")]
            if {"message_id"} not in unique_sets:
                raise RuntimeError("research A2A message_id uniqueness missing")
            checks = {row.get("name") for row in schema.get_check_constraints(tasks.name)}
            if "research_a2a_tasks_state_check" not in checks:
                raise RuntimeError("research A2A state constraint missing")

        async with self.engine.connect() as connection:
            await connection.run_sync(check)

    async def dispose(self) -> None:
        await self.engine.dispose()

    async def reserve(self, message_id: UUID, submission: dict[str, Any], commitment: str) -> tuple[StoredTask, bool]:
        now = datetime.now(UTC)
        values = {
            "id": str(uuid4()), "message_id": str(message_id), "submission": submission,
            "submission_commitment": commitment, "state": "submitted", "artifact": None,
            "error": None, "run_count": 0, "run_id": None, "lease_owner": None,
            "lease_expires_at": None, "created_at": now, "updated_at": now,
        }
        statement = (postgresql_insert(tasks) if self.engine.dialect.name == "postgresql" else sqlite_insert(tasks)).values(**values)
        statement = statement.on_conflict_do_nothing(index_elements=[tasks.c.message_id])
        async with self.engine.begin() as connection:
            result = await connection.execute(statement)
            created = result.rowcount == 1
        async with self.engine.connect() as connection:
            row = (await connection.execute(select(tasks).where(tasks.c.message_id == str(message_id)))).one()
        task = _stored(row)
        if task.submission_commitment != commitment:
            raise SubmissionConflict("messageId is already bound to a different submission")
        return task, created

    async def get(self, task_id: UUID) -> StoredTask:
        async with self.engine.connect() as connection:
            row = (await connection.execute(select(tasks).where(tasks.c.id == str(task_id)))).one_or_none()
        if row is None:
            raise TaskNotFound(str(task_id))
        return _stored(row)

    async def submission(self, task_id: UUID) -> dict[str, Any]:
        async with self.engine.connect() as connection:
            value = await connection.scalar(select(tasks.c.submission).where(tasks.c.id == str(task_id)))
        if value is None:
            raise TaskNotFound(str(task_id))
        return value

    async def claim(self, task_id: UUID, owner: str, *, lease_seconds: float) -> UUID | None:
        now, run_id = datetime.now(UTC), uuid4()
        async with self.engine.begin() as connection:
            result = await connection.execute(
                update(tasks).where(tasks.c.id == str(task_id), tasks.c.state == "submitted").values(
                    state="working", run_count=tasks.c.run_count + 1, run_id=str(run_id),
                    lease_owner=owner, lease_expires_at=now + timedelta(seconds=lease_seconds), updated_at=now,
                )
            )
        return run_id if result.rowcount == 1 else None

    async def heartbeat(self, task_id: UUID, owner: str, run_id: UUID, *, lease_seconds: float) -> bool:
        now = datetime.now(UTC)
        async with self.engine.begin() as connection:
            result = await connection.execute(
                update(tasks).where(
                    tasks.c.id == str(task_id), tasks.c.state == "working",
                    tasks.c.lease_owner == owner, tasks.c.run_id == str(run_id),
                ).values(lease_expires_at=now + timedelta(seconds=lease_seconds), updated_at=now)
            )
        return result.rowcount == 1

    async def complete(self, task_id: UUID, owner: str, run_id: UUID, artifact: dict[str, Any]) -> StoredTask:
        await self._terminal_update(task_id, owner, run_id, "completed", artifact=artifact)
        return await self.get(task_id)

    async def fail(self, task_id: UUID, owner: str, run_id: UUID, error: str) -> StoredTask:
        await self._terminal_update(task_id, owner, run_id, "failed", error=error)
        return await self.get(task_id)

    async def _terminal_update(self, task_id: UUID, owner: str, run_id: UUID, state: str, *, artifact=None, error=None) -> None:
        async with self.engine.begin() as connection:
            await connection.execute(
                update(tasks).where(
                    tasks.c.id == str(task_id), tasks.c.state == "working",
                    tasks.c.lease_owner == owner, tasks.c.run_id == str(run_id),
                ).values(state=state, artifact=artifact, error=error, lease_owner=None,
                         lease_expires_at=None, updated_at=datetime.now(UTC))
            )

    async def cancel(self, task_id: UUID) -> StoredTask:
        async with self.engine.begin() as connection:
            await connection.execute(
                update(tasks).where(tasks.c.id == str(task_id), tasks.c.state.in_(("submitted", "working"))).values(
                    state="canceled", artifact=None, lease_owner=None, lease_expires_at=None,
                    updated_at=datetime.now(UTC))
            )
        return await self.get(task_id)

    async def recoverable(self) -> list[UUID]:
        now = datetime.now(UTC)
        async with self.engine.begin() as connection:
            await connection.execute(
                update(tasks).where(tasks.c.state == "working", tasks.c.lease_expires_at.is_not(None),
                                    tasks.c.lease_expires_at <= now).values(
                    state="failed", error="research worker lease expired; manual reconciliation required",
                    lease_owner=None, lease_expires_at=None, updated_at=now)
            )
            rows = (await connection.execute(select(tasks.c.id).where(tasks.c.state == "submitted").order_by(tasks.c.created_at))).scalars().all()
        return [UUID(task_id) for task_id in rows]

    async def run_count(self, task_id: UUID) -> int:
        async with self.engine.connect() as connection:
            value = await connection.scalar(select(tasks.c.run_count).where(tasks.c.id == str(task_id)))
        if value is None:
            raise TaskNotFound(str(task_id))
        return value
