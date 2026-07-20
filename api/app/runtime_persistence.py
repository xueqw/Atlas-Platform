"""SQLAlchemy repositories for the additive Atlas runtime ledger.

The Phase 1 service deliberately keeps its domain contract independent from
SQLAlchemy. This module is the production-shaped adapter: run idempotency and
event sequence allocation happen against the durable runtime tables rather
than the process-local test repositories.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hmac
import json
from typing import Callable
import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import RuntimeEvent as DbRuntimeEvent
from .models import RuntimeInterrupt as DbRuntimeInterrupt
from .models import RuntimeRun as DbRuntimeRun
from .runtime_contract import (
    RuntimeAccessDenied,
    RuntimeEvent,
    RuntimeInterruptCreate,
    RuntimeInterruptRecord,
    RuntimeResumeRequest,
    RuntimeStartRequest,
    RuntimeStatus,
    RuntimeTransition,
)
from .runtime_events import RuntimeEventRepository
from .runtime_graph import AtlasAgentState
from .runtime_projection import ensure_workflow_run, project_runtime_event
from .runtime_service import RuntimeInterruptRepository, RuntimeRunRecord, RuntimeRunRepository


def _request_json(request: RuntimeStartRequest) -> str:
    return json.dumps(request.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class SqlAlchemyRuntimeRunRepository(RuntimeRunRepository):
    """Persist runtime state and idempotency keys in ``runtime_runs``."""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory

    def create_or_get(self, request: RuntimeStartRequest, execution_mode: str) -> tuple[RuntimeRunRecord, bool]:
        expected_request = _request_json(request)
        with self._session_factory() as db:
            existing = db.scalar(select(DbRuntimeRun).where(
                DbRuntimeRun.workspace_id == request.workspace_id,
                DbRuntimeRun.idempotency_key == request.idempotency_key,
            ))
            if existing is not None:
                if existing.input_json != expected_request:
                    raise ValueError("idempotency key was already used with a different request")
                return self._record(existing), False

            run_id = str(uuid.uuid4())
            state = AtlasAgentState.initial(
                request.make_identity(run_id), request.input,
                requested_execution_strategy=request.execution_strategy.value,
                requested_resources=request.requested_resources,
            )
            row = DbRuntimeRun(
                id=run_id,
                workspace_id=request.workspace_id,
                user_id=request.user_id,
                agent_id=request.agent_id,
                conversation_id=request.conversation_id,
                version_id=request.agent_version_id,
                source=request.source.value,
                graph_template="phase1-react",
                execution_mode=execution_mode,
                thread_id=state.identity.thread_id,
                idempotency_key=request.idempotency_key,
                status=state.status.value,
                input_json=expected_request,
                state_json=state.model_dump_json(),
                started_at=datetime.now(timezone.utc),
            )
            db.add(row)
            try:
                db.flush()
                ensure_workflow_run(db, row, request)
                db.commit()
            except IntegrityError:
                # A concurrent start won the unique idempotency key. Return its
                # record only when the payload matches exactly.
                db.rollback()
                row = db.scalar(select(DbRuntimeRun).where(
                    DbRuntimeRun.workspace_id == request.workspace_id,
                    DbRuntimeRun.idempotency_key == request.idempotency_key,
                ))
                if row is None or row.input_json != expected_request:
                    raise
                return self._record(row), False
            db.refresh(row)
            return self._record(row), True

    def get(self, *, run_id: str, workspace_id: str) -> RuntimeRunRecord:
        with self._session_factory() as db:
            row = db.scalar(select(DbRuntimeRun).where(
                DbRuntimeRun.id == run_id,
                DbRuntimeRun.workspace_id == workspace_id,
            ))
            if row is None:
                raise RuntimeAccessDenied()
            return self._record(row)

    def save(self, record: RuntimeRunRecord) -> None:
        run_id = record.state.identity.run_id
        with self._session_factory() as db:
            row = db.scalar(select(DbRuntimeRun).where(
                DbRuntimeRun.id == run_id,
                DbRuntimeRun.workspace_id == record.state.identity.workspace_id,
            ).with_for_update())
            if row is None:
                raise RuntimeAccessDenied()
            row.state_json = record.state.model_dump_json()
            row.status = record.state.status.value
            row.execution_mode = record.execution_mode
            if record.state.execution_strategy:
                row.graph_template = record.state.execution_strategy
            row.error = json.dumps(record.state.errors[-1], ensure_ascii=False) if record.state.errors else ""
            if record.state.status in {RuntimeStatus.SUCCEEDED, RuntimeStatus.FAILED, RuntimeStatus.CANCELLED}:
                row.ended_at = datetime.now(timezone.utc)
            db.commit()

    @staticmethod
    def _record(row: DbRuntimeRun) -> RuntimeRunRecord:
        request = RuntimeStartRequest.model_validate_json(row.input_json)
        state = AtlasAgentState.model_validate_json(row.state_json)
        return RuntimeRunRecord(
            request=request,
            state=state,
            execution_mode=row.execution_mode or "legacy",
            created_at=row.created_at,
        )


class SqlAlchemyRuntimeEventRepository(RuntimeEventRepository):
    """Durable, tenant-scoped event ledger with per-run monotonic ordering."""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory

    def append(self, *, run_id: str, workspace_id: str, transition: RuntimeTransition,
               event_id: str | None = None, timestamp: datetime | None = None) -> RuntimeEvent:
        with self._session_factory() as db:
            # Row-level locking serializes sequence allocation on PostgreSQL. On
            # SQLite the write transaction still serializes writers for local dev.
            run = db.scalar(select(DbRuntimeRun).where(
                DbRuntimeRun.id == run_id,
                DbRuntimeRun.workspace_id == workspace_id,
            ).with_for_update())
            if run is None:
                raise RuntimeAccessDenied()
            run.last_sequence += 1
            created_at = timestamp or datetime.now(timezone.utc)
            row = DbRuntimeEvent(
                run_id=run_id,
                event_id=event_id or str(uuid.uuid4()),
                sequence=run.last_sequence,
                type=transition.event_type,
                payload_json=json.dumps(transition.payload, ensure_ascii=False, sort_keys=True),
                created_at=created_at,
            )
            db.add(row)
            db.flush()
            project_runtime_event(
                db,
                runtime_run=run,
                event_type=row.type,
                sequence=row.sequence,
                payload=transition.payload,
                timestamp=created_at,
            )
            db.commit()
            return RuntimeEvent(
                event_id=row.event_id,
                run_id=run_id,
                workspace_id=workspace_id,
                sequence=row.sequence,
                timestamp=row.created_at,
                type=row.type,
                payload=json.loads(row.payload_json),
            )

    def replay(self, *, run_id: str, workspace_id: str, after_sequence: int = 0) -> tuple[RuntimeEvent, ...]:
        if after_sequence < 0:
            raise ValueError("after_sequence must be non-negative")
        with self._session_factory() as db:
            exists = db.scalar(select(DbRuntimeRun.id).where(
                DbRuntimeRun.id == run_id,
                DbRuntimeRun.workspace_id == workspace_id,
            ))
            if exists is None:
                raise RuntimeAccessDenied()
            rows = db.scalars(select(DbRuntimeEvent).where(
                DbRuntimeEvent.run_id == run_id,
                DbRuntimeEvent.sequence > after_sequence,
            ).order_by(DbRuntimeEvent.sequence)).all()
            return tuple(RuntimeEvent(
                event_id=row.event_id,
                run_id=run_id,
                workspace_id=workspace_id,
                sequence=row.sequence,
                timestamp=row.created_at,
                type=row.type,
                payload=json.loads(row.payload_json),
            ) for row in rows)


class SqlAlchemyRuntimeInterruptRepository(RuntimeInterruptRepository):
    """Atomic, tenant-scoped interrupt decisions with nonce anti-replay."""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory

    def create(self, request: RuntimeInterruptCreate) -> RuntimeInterruptRecord:
        now = datetime.now(timezone.utc)
        if request.expires_at <= now:
            raise ValueError("interrupt expiry must be in the future")
        with self._session_factory() as db:
            run = db.scalar(select(DbRuntimeRun).where(
                DbRuntimeRun.id == request.run_id,
                DbRuntimeRun.workspace_id == request.workspace_id,
                DbRuntimeRun.user_id == request.user_id,
            ))
            if run is None:
                raise RuntimeAccessDenied()
            row = DbRuntimeInterrupt(
                id=str(uuid.uuid4()),
                run_id=request.run_id,
                workspace_id=request.workspace_id,
                user_id=request.user_id,
                status="pending",
                payload_json=json.dumps(
                    {"scope": list(request.scope), "payload": request.payload},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                parameter_digest=request.parameter_digest,
                resource_version=request.resource_version,
                nonce=request.nonce,
                expires_at=request.expires_at,
            )
            db.add(row)
            try:
                db.commit()
            except IntegrityError as exc:
                db.rollback()
                raise ValueError("interrupt nonce was already used for this run") from exc
            db.refresh(row)
            return self._record(row)

    def resolve(self, request: RuntimeResumeRequest) -> RuntimeInterruptRecord:
        if request.interrupt_id is None:
            raise ValueError("interrupt_id is required")
        with self._session_factory() as db:
            row = db.scalar(select(DbRuntimeInterrupt).where(
                DbRuntimeInterrupt.id == request.interrupt_id,
                DbRuntimeInterrupt.run_id == request.run_id,
                DbRuntimeInterrupt.workspace_id == request.workspace_id,
                DbRuntimeInterrupt.user_id == request.user_id,
            ).with_for_update())
            if row is None:
                raise RuntimeAccessDenied()
            now = datetime.now(timezone.utc)
            if row.status != "pending":
                raise ValueError("interrupt decision was already consumed")
            expires_at = row.expires_at
            if expires_at is None or _aware(expires_at) <= now:
                row.status = "expired"
                row.resolved_at = now
                db.commit()
                raise ValueError("interrupt decision has expired")
            if not (
                hmac.compare_digest(row.nonce, request.nonce or "")
                and hmac.compare_digest(row.parameter_digest, request.parameter_digest or "")
                and hmac.compare_digest(row.resource_version, request.resource_version or "")
            ):
                raise RuntimeAccessDenied()
            row.status = "approved" if request.decision == "approve" else "denied"
            row.resolved_at = now
            db.commit()
            db.refresh(row)
            return self._record(row)

    def get_by_nonce(
        self, *, run_id: str, workspace_id: str, user_id: str, nonce: str,
    ) -> RuntimeInterruptRecord | None:
        with self._session_factory() as db:
            row = db.scalar(select(DbRuntimeInterrupt).where(
                DbRuntimeInterrupt.run_id == run_id,
                DbRuntimeInterrupt.workspace_id == workspace_id,
                DbRuntimeInterrupt.user_id == user_id,
                DbRuntimeInterrupt.nonce == nonce,
            ))
            return self._record(row) if row is not None else None

    @staticmethod
    def _record(row: DbRuntimeInterrupt) -> RuntimeInterruptRecord:
        data = json.loads(row.payload_json or "{}")
        return RuntimeInterruptRecord(
            interrupt_id=row.id,
            run_id=row.run_id,
            workspace_id=row.workspace_id,
            user_id=row.user_id,
            status=row.status,
            parameter_digest=row.parameter_digest,
            resource_version=row.resource_version,
            scope=tuple(data.get("scope") or ()),
            nonce=row.nonce,
            expires_at=_aware(row.expires_at),
            payload=data.get("payload") or {},
            resolved_at=_aware(row.resolved_at) if row.resolved_at else None,
        )


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
