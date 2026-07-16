"""SQLAlchemy repositories for the additive Atlas runtime ledger.

The Phase 1 service deliberately keeps its domain contract independent from
SQLAlchemy. This module is the production-shaped adapter: run idempotency and
event sequence allocation happen against the durable runtime tables rather
than the process-local test repositories.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Callable
import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import RuntimeEvent as DbRuntimeEvent
from .models import RuntimeRun as DbRuntimeRun
from .runtime_contract import RuntimeAccessDenied, RuntimeEvent, RuntimeStartRequest, RuntimeStatus, RuntimeTransition
from .runtime_events import RuntimeEventRepository
from .runtime_graph import AtlasAgentState
from .runtime_service import RuntimeRunRecord, RuntimeRunRepository


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
            state = AtlasAgentState.initial(request.make_identity(run_id), request.input)
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
