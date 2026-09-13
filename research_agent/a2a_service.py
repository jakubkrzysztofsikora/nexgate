"""Authenticated durable A2A service for Lustro research submissions."""

from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
from collections.abc import Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from google.protobuf.json_format import ParseDict, ParseError
from pydantic import Field, ValidationError, field_validator, model_validator

from a2a.types import a2a_pb2

from .a2a_store import LeaseLost, SQLAlchemyA2ATaskStore, StoredTask, SubmissionConflict, TaskNotFound
from .models import ClusterResearchDraftRequest, ProductPolicy, ResearchEvidenceRecord, StrictModel
from .policy import canonical_bytes, validate_request
from .research import ResearchRun, research_cluster


class LustroResearchSubmission(StrictModel):
    schema_version: Literal[1] = 1
    request: ClusterResearchDraftRequest
    policy: ProductPolicy
    seed_records: Annotated[
        list[ResearchEvidenceRecord], Field(strict=True, min_length=1, max_length=64)
    ]

    @field_validator("schema_version", mode="before")
    @classmethod
    def integer_version(cls, value: Any) -> Any:
        if type(value) is not int:
            raise ValueError("schema_version must be an integer")
        return value

    @model_validator(mode="after")
    def trusted_context_matches_request(self) -> "LustroResearchSubmission":
        validate_request(self.request, self.policy)
        records = {record.evidence_id: record for record in self.seed_records}
        if len(records) != len(self.seed_records):
            raise ValueError("duplicate seed evidence")
        required = set(self.request.authority_evidence_ids)
        required.update(record.evidence_id for record in self.request.representatives)
        if not required <= records.keys():
            raise ValueError("missing trusted authority or representative seed records")
        for authority in self.policy.evidence_authorities:
            record = records.get(authority.evidence_id)
            if record is None:
                continue
            digest = hashlib.sha256(canonical_bytes(record)).hexdigest()
            if authority.record_sha256 != digest:
                raise ValueError("seed evidence does not match host authority commitment")
        return self


class TaskSnapshot(StrictModel):
    id: UUID
    message_id: UUID
    state: Literal["submitted", "working", "completed", "failed", "canceled"]
    artifacts: Annotated[list[ResearchRun], Field(max_length=1)] = Field(default_factory=list)
    error: str | None = None
    cancellation_pending: bool = False


class _RunStopped(Exception):
    """The owner reached a durable checkpoint and must unwind execution."""


ResearchExecutor = Callable[
    [
        ClusterResearchDraftRequest,
        ProductPolicy,
        tuple[ResearchEvidenceRecord, ...],
        Callable[[], Awaitable[None]],
    ],
    Awaitable[ResearchRun],
]
Enqueue = Callable[[UUID], Awaitable[None]]


def _restore_json_integers(value: Any) -> Any:
    """Recover JSON integers coerced to doubles by protobuf Value."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, list):
        return [_restore_json_integers(item) for item in value]
    if isinstance(value, dict):
        return {key: _restore_json_integers(item) for key, item in value.items()}
    return value


def _snapshot(task: StoredTask) -> TaskSnapshot:
    artifacts = [] if task.artifact is None else [ResearchRun.model_validate(task.artifact)]
    return TaskSnapshot(
        id=task.id,
        message_id=task.message_id,
        state=task.state,
        artifacts=artifacts,
        error=task.error,
        cancellation_pending=task.state == "working" and task.cancel_requested,
    )


class DurableA2AService:
    def __init__(self, store: SQLAlchemyA2ATaskStore, *, enqueue: Enqueue, owner_id: str | None = None, lease_seconds: float = 360):
        self.store = store
        self.enqueue = enqueue
        self.owner_id = owner_id or str(uuid4())
        self.lease_seconds = lease_seconds
        self._executions: dict[UUID, asyncio.Task] = {}
        self.worker_healthy = True

    async def submit(
        self, message_id: UUID, submission: LustroResearchSubmission
    ) -> TaskSnapshot:
        submission = LustroResearchSubmission.model_validate(submission.model_dump())
        if message_id != submission.request.request_id:
            raise ValueError("messageId must equal request.request_id")
        payload = submission.model_dump(mode="json")
        commitment = hashlib.sha256(canonical_bytes(submission)).hexdigest()
        task, created = await self.store.reserve(message_id, payload, commitment)
        # Reservation is durable but the local queue is not. A resend must
        # recover a submitted reservation if the original queue handoff failed.
        if task.state == "submitted":
            await self.enqueue(task.id)
        return _snapshot(task)

    async def get(self, task_id: UUID) -> TaskSnapshot:
        return _snapshot(await self.store.get(task_id))

    async def cancel(self, task_id: UUID) -> TaskSnapshot:
        task = await self.store.cancel(task_id)
        return _snapshot(task)

    async def recover_pending(self) -> int:
        task_ids = await self.store.recoverable()
        for task_id in task_ids:
            await self.enqueue(task_id)
        return len(task_ids)

    async def execute(self, task_id: UUID, research: ResearchExecutor) -> TaskSnapshot:
        run_id = await self.store.claim(task_id, self.owner_id, lease_seconds=self.lease_seconds)
        if run_id is None:
            return await self.get(task_id)
        execution = asyncio.current_task()
        if execution is not None:
            self._executions[task_id] = execution
        heartbeat = asyncio.create_task(self._heartbeat(task_id, run_id))

        async def checkpoint() -> None:
            if not await self.store.run_active(task_id, self.owner_id, run_id):
                raise _RunStopped

        try:
            submission = LustroResearchSubmission.model_validate(
                await self.store.submission(task_id)
            )
            result = await research(
                submission.request,
                submission.policy,
                tuple(submission.seed_records),
                checkpoint,
            )
            result = ResearchRun.model_validate(
                result.model_dump() if isinstance(result, ResearchRun) else result
            )
            task = await self.store.complete(task_id, self.owner_id, run_id, result.model_dump(mode="json"))
        except (LeaseLost, _RunStopped):
            task = await self.store.acknowledge_cancellation(task_id, self.owner_id, run_id)
        except asyncio.CancelledError:
            # Cancellation interrupts an await but not a to_thread provider
            # call. Leave the run unacknowledged for lease-expiry
            # reconciliation, and propagate so worker shutdown and the runtime
            # lifespan can complete.
            raise
        except TimeoutError:
            # Leave this run unacknowledged for lease-expiry reconciliation.
            task = await self.store.get(task_id)
        except Exception:
            task = await self.store.get(task_id)
            if task.cancel_requested:
                task = await self.store.acknowledge_cancellation(
                    task_id, self.owner_id, run_id
                )
            else:
                try:
                    task = await self.store.fail(
                        task_id,
                        self.owner_id,
                        run_id,
                        "research execution failed",
                    )
                except LeaseLost:
                    task = await self.store.acknowledge_cancellation(task_id, self.owner_id, run_id)
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            self._executions.pop(task_id, None)
        if task.state == "working":
            await self.store.recoverable()
            task = await self.store.get(task_id)
        return _snapshot(task)

    async def _heartbeat(self, task_id: UUID, run_id: UUID) -> None:
        interval = max(0.1, min(1.0, self.lease_seconds / 3))
        while True:
            await asyncio.sleep(interval)
            if not await self.store.heartbeat(task_id, self.owner_id, run_id, lease_seconds=self.lease_seconds):
                return


async def run_worker(
    service: DurableA2AService,
    queue: asyncio.Queue[UUID],
    research: ResearchExecutor,
    *,
    retry_delay: float = 0.1,
) -> None:
    """Keep a transient store failure from silently killing the sole worker."""
    service.worker_healthy = True
    while True:
        task_id = await queue.get()
        try:
            await service.execute(task_id, research)
            service.worker_healthy = True
        except asyncio.CancelledError:
            raise
        except Exception:
            service.worker_healthy = False
            await asyncio.sleep(retry_delay)
            await queue.put(task_id)
        finally:
            queue.task_done()


def _a2a_task(snapshot: TaskSnapshot) -> dict[str, Any]:
    states = {
        "submitted": "TASK_STATE_SUBMITTED",
        "working": "TASK_STATE_WORKING",
        "completed": "TASK_STATE_COMPLETED",
        "failed": "TASK_STATE_FAILED",
        "canceled": "TASK_STATE_CANCELED",
    }
    task: dict[str, Any] = {
        "id": str(snapshot.id),
        "contextId": str(snapshot.message_id),
        "status": {"state": states[snapshot.state]},
    }
    status_message = snapshot.error
    if snapshot.cancellation_pending:
        status_message = "Cancellation pending owner acknowledgement"
    if status_message:
        task["status"]["message"] = {
            "messageId": f"{snapshot.id}-error",
            "role": "ROLE_AGENT",
            "parts": [{"text": status_message}],
        }
    if snapshot.artifacts:
        task["artifacts"] = [
            {
                "artifactId": f"{snapshot.id}-research-run",
                "name": "ResearchRun",
                "parts": [
                    {
                        "data": snapshot.artifacts[0].model_dump(mode="json"),
                    }
                ],
            }
        ]
    return task


def create_app(
    service: DurableA2AService,
    *,
    bearer_token: str,
    lifespan: Callable[[FastAPI], Any] | None = None,
) -> FastAPI:
    if not bearer_token:
        raise ValueError("Lustro A2A bearer token is required")
    security = HTTPBearer(auto_error=False)

    async def authenticate(
        credentials: HTTPAuthorizationCredentials | None = Depends(security),
    ) -> None:
        if (
            credentials is None
            or credentials.scheme.lower() != "bearer"
            or not secrets.compare_digest(credentials.credentials, bearer_token)
        ):
            raise HTTPException(status_code=401, detail="unauthorized")

    app = FastAPI(title="NexGate private Lustro research agent", lifespan=lifespan)

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        if not service.worker_healthy:
            raise HTTPException(status_code=503, detail="research worker unavailable")
        return {"status": "ok"}

    @app.get("/.well-known/agent-card.json", dependencies=[Depends(authenticate)])
    async def agent_card() -> dict[str, Any]:
        return {
            "name": "quick-research",
            "description": "Private durable Lustro research task service",
            "supportedInterfaces": [
                {
                    "url": os.environ.get(
                        "A2A_AGENT_URL", "http://research-agent:9000/"
                    ),
                    "protocolBinding": "JSONRPC",
                    "protocolVersion": "1.0",
                }
            ],
            "version": "1.0.0",
            "capabilities": {"streaming": False},
            "defaultInputModes": ["application/json"],
            "defaultOutputModes": ["application/json"],
            "securitySchemes": {
                "lustroBearer": {
                    "httpAuthSecurityScheme": {"scheme": "bearer"}
                }
            },
            "securityRequirements": [
                {"schemes": {"lustroBearer": {"list": []}}}
            ],
            "skills": [
                {
                    "id": "cluster-research-draft",
                    "name": "cluster research draft",
                    "description": "Run bounded research from a trusted Lustro submission",
                    "tags": ["research", "lustro"],
                }
            ],
        }

    @app.post("/", dependencies=[Depends(authenticate)])
    async def jsonrpc(
        body: dict[str, Any],
        a2a_version: Annotated[str | None, Header(alias="A2A-Version")] = None,
    ) -> dict[str, Any]:
        request_id = body.get("id")
        response = {"jsonrpc": "2.0", "id": request_id}
        try:
            if body.get("jsonrpc") != "2.0":
                raise ValueError("invalid JSON-RPC version")
            if a2a_version != "1.0":
                return response | {
                    "error": {"code": -32009, "message": "Version not supported"}
                }
            method = body.get("method")
            params = body.get("params")
            if not isinstance(params, dict):
                raise ValueError("params must be an object")
            if method == "SendMessage":
                request = ParseDict(params, a2a_pb2.SendMessageRequest())
                message = request.message
                if (
                    message.role != a2a_pb2.ROLE_USER
                    or len(message.parts) != 1
                    or message.parts[0].WhichOneof("content") != "data"
                ):
                    raise ValueError("one user DataPart is required")
                message_id = UUID(message.message_id)
                submission = LustroResearchSubmission.model_validate(
                    _restore_json_integers(params["message"]["parts"][0]["data"])
                )
                result = await service.submit(message_id, submission)
                return response | {"result": {"task": _a2a_task(result)}}
            if method == "GetTask":
                request = ParseDict(params, a2a_pb2.GetTaskRequest())
                result = await service.get(UUID(request.id))
            elif method == "CancelTask":
                request = ParseDict(params, a2a_pb2.CancelTaskRequest())
                result = await service.cancel(UUID(request.id))
            else:
                return response | {
                    "error": {"code": -32601, "message": "Method not found"}
                }
            return response | {"result": _a2a_task(result)}
        except TaskNotFound:
            return response | {"error": {"code": -32001, "message": "Task not found"}}
        except SubmissionConflict:
            return response | {"error": {"code": -32010, "message": "Message ID conflict"}}
        except (KeyError, TypeError, ValueError, ValidationError, ParseError):
            return response | {"error": {"code": -32602, "message": "Invalid params"}}

    return app


def validate_runtime_configuration(environment: Mapping[str, str]) -> None:
    required = (
        "A2A_DATABASE_URL", "LUSTRO_A2A_BEARER_TOKEN", "RESEARCH_ARCHIVE_BUCKET",
        "RESEARCH_ARCHIVE_ENDPOINT_URL", "LITELLM_API_KEY", "TAVILY_API_KEY",
    )
    for name in required:
        value = environment.get(name, "").strip()
        if not value or "replace-with" in value:
            raise RuntimeError(f"{name} must be configured before research-agent startup")
    if not environment["A2A_DATABASE_URL"].startswith("postgresql+asyncpg://"):
        raise RuntimeError("A2A_DATABASE_URL must use postgresql+asyncpg")
    if not environment["RESEARCH_ARCHIVE_ENDPOINT_URL"].startswith("https://"):
        raise RuntimeError("RESEARCH_ARCHIVE_ENDPOINT_URL must use HTTPS")


def app_from_env() -> FastAPI:
    """Uvicorn factory for the opt-in private research service container."""
    validate_runtime_configuration(os.environ)
    database_url = os.environ["A2A_DATABASE_URL"]
    bearer_token = os.environ["LUSTRO_A2A_BEARER_TOKEN"]
    archive_bucket = os.environ["RESEARCH_ARCHIVE_BUCKET"]
    store = SQLAlchemyA2ATaskStore.from_url(database_url)
    queue: asyncio.Queue[UUID] = asyncio.Queue()

    async def enqueue(task_id: UUID) -> None:
        await queue.put(task_id)

    service = DurableA2AService(store, enqueue=enqueue)

    async def configured_research(request, policy, seed_records, checkpoint):
        import boto3

        from .capture import EvidenceStore, S3Backend
        from .model_litellm import LiteLLMModel
        from .search_tavily import TavilySearch

        client_kwargs = {}
        if endpoint := os.environ.get("RESEARCH_ARCHIVE_ENDPOINT_URL"):
            client_kwargs["endpoint_url"] = endpoint
        s3_client = boto3.client("s3", **client_kwargs)
        evidence_store = EvidenceStore(
            S3Backend(s3_client, archive_bucket),
            seed_records=seed_records,
            max_bytes=policy.max_source_bytes,
        )
        return await research_cluster(
            request,
            policy,
            TavilySearch(),
            LiteLLMModel(),
            evidence_store,
            checkpoint,
        )

    @asynccontextmanager
    async def runtime_lifespan(_app: FastAPI):
        await store.assert_schema_ready()
        worker_task = asyncio.create_task(run_worker(service, queue, configured_research))
        await service.recover_pending()
        try:
            yield
        finally:
            worker_task.cancel()
            await asyncio.gather(worker_task, return_exceptions=True)
            await store.dispose()

    return create_app(service, bearer_token=bearer_token, lifespan=runtime_lifespan)
