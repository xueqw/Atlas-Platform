"""Feature-flagged Runtime service with in-memory repositories for Phase 1."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from threading import RLock
from typing import Callable, Protocol
import os
import uuid

from .runtime_contract import (
    RuntimeAccessDenied,
    RuntimeEvent,
    RuntimeHandle,
    RuntimeResumeRequest,
    RuntimeStartRequest,
    RuntimeState,
    RuntimeStatus,
    RuntimeTransition,
    classify_error,
    utcnow,
)
from .runtime_events import InMemoryRuntimeEventRepository, RuntimeEventRepository
from .runtime_graph import AtlasAgentState, GraphExecutionResult, LegacyGraphShim, RuntimePhaseOneGraph


class RuntimeRunner(Protocol):
    async def ainvoke(self, state: AtlasAgentState) -> GraphExecutionResult: ...


class LegacyRuntimeAdapter(Protocol):
    async def run(self, request: RuntimeStartRequest, state: AtlasAgentState) -> GraphExecutionResult: ...


class GraphLegacyAdapter:
    """Adapter seam for wiring the existing workbench runtime during Phase 3."""

    def __init__(self, graph: RuntimeRunner | None = None) -> None:
        self.graph = graph or LegacyGraphShim(lambda _state: "Legacy runtime adapter is not configured.")

    async def run(self, _request: RuntimeStartRequest, state: AtlasAgentState) -> GraphExecutionResult:
        return await self.graph.ainvoke(state)


@dataclass(frozen=True)
class RuntimeFeatureFlags:
    langgraph_enabled: bool = False
    allow_legacy_fallback: bool = True

    @classmethod
    def from_environment(cls) -> "RuntimeFeatureFlags":
        enabled = os.getenv("LANGGRAPH_RUNTIME_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
        fallback = os.getenv("LANGGRAPH_RUNTIME_LEGACY_FALLBACK", "true").strip().lower() in {"1", "true", "yes", "on"}
        return cls(langgraph_enabled=enabled, allow_legacy_fallback=fallback)


@dataclass(frozen=True)
class RuntimeRunRecord:
    request: RuntimeStartRequest
    state: AtlasAgentState
    execution_mode: str
    created_at: datetime


class RuntimeRunRepository(Protocol):
    def create_or_get(self, request: RuntimeStartRequest, execution_mode: str) -> tuple[RuntimeRunRecord, bool]: ...
    def get(self, *, run_id: str, workspace_id: str) -> RuntimeRunRecord: ...
    def save(self, record: RuntimeRunRecord) -> None: ...


class InMemoryRuntimeRunRepository:
    """Reference idempotency store; replace with a unique DB key in production."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._records: dict[str, RuntimeRunRecord] = {}
        self._idempotency: dict[tuple[str, str], tuple[str, str]] = {}

    def create_or_get(self, request: RuntimeStartRequest, execution_mode: str) -> tuple[RuntimeRunRecord, bool]:
        fingerprint = request.model_dump_json()
        key = (request.workspace_id, request.idempotency_key)
        with self._lock:
            existing = self._idempotency.get(key)
            if existing:
                run_id, stored_fingerprint = existing
                if stored_fingerprint != fingerprint:
                    raise ValueError("idempotency key was already used with a different request")
                return self._records[run_id], False
            run_id = str(uuid.uuid4())
            state = AtlasAgentState.initial(request.make_identity(run_id), request.input)
            record = RuntimeRunRecord(request=request, state=state, execution_mode=execution_mode, created_at=utcnow())
            self._records[run_id] = record
            self._idempotency[key] = (run_id, fingerprint)
            return record, True

    def get(self, *, run_id: str, workspace_id: str) -> RuntimeRunRecord:
        with self._lock:
            record = self._records.get(run_id)
            if record is None or record.state.identity.workspace_id != workspace_id:
                raise RuntimeAccessDenied()
            return record

    def save(self, record: RuntimeRunRecord) -> None:
        with self._lock:
            self._records[record.state.identity.run_id] = record


class AgentRuntimeService:
    """Public Phase 1 entry point. All client-visible state moves through Runtime Events."""

    def __init__(self, graph: RuntimeRunner, *, event_repository: RuntimeEventRepository | None = None,
                 run_repository: RuntimeRunRepository | None = None, legacy_adapter: LegacyRuntimeAdapter | None = None,
                 flags: RuntimeFeatureFlags | None = None) -> None:
        self.graph = graph
        self.events = event_repository or InMemoryRuntimeEventRepository()
        self.runs = run_repository or InMemoryRuntimeRunRepository()
        self.legacy_adapter = legacy_adapter or GraphLegacyAdapter()
        self.flags = flags or RuntimeFeatureFlags.from_environment()

    async def start(self, request: RuntimeStartRequest) -> RuntimeHandle:
        mode = "langgraph" if self.flags.langgraph_enabled else "legacy"
        record, created = self.runs.create_or_get(request, mode)
        if not created:
            return self._handle(record)
        self._emit(record, RuntimeTransition(event_type="run.started", payload={"mode": mode}))
        return await self._execute(record)

    def stream(self, *, run_id: str, workspace_id: str, after_sequence: int = 0) -> tuple[RuntimeEvent, ...]:
        return self.events.replay(run_id=run_id, workspace_id=workspace_id, after_sequence=after_sequence)

    async def resume(self, request: RuntimeResumeRequest) -> RuntimeHandle:
        record = self.runs.get(run_id=request.run_id, workspace_id=request.workspace_id)
        self._assert_actor(record, request.user_id)
        if record.state.status in {RuntimeStatus.SUCCEEDED, RuntimeStatus.FAILED, RuntimeStatus.CANCELLED}:
            return self._handle(record)
        resumed = RuntimeRunRecord(record.request, self._state_with(record.state, status=RuntimeStatus.RUNNING), record.execution_mode, record.created_at)
        self.runs.save(resumed)
        self._emit(resumed, RuntimeTransition(event_type="run.resumed", payload={}))
        return await self._execute(resumed)

    def cancel(self, *, run_id: str, workspace_id: str, user_id: str) -> RuntimeHandle:
        record = self.runs.get(run_id=run_id, workspace_id=workspace_id)
        self._assert_actor(record, user_id)
        if record.state.status not in {RuntimeStatus.SUCCEEDED, RuntimeStatus.FAILED, RuntimeStatus.CANCELLED}:
            record = RuntimeRunRecord(record.request, self._state_with(record.state, status=RuntimeStatus.CANCELLED), record.execution_mode, record.created_at)
            self.runs.save(record)
            self._emit(record, RuntimeTransition(event_type="run.cancelled", payload={}))
        return self._handle(record)

    def get_state(self, *, run_id: str, workspace_id: str) -> RuntimeState:
        return self.runs.get(run_id=run_id, workspace_id=workspace_id).state.to_runtime_state()

    def get_history(self, *, run_id: str, workspace_id: str) -> tuple[RuntimeEvent, ...]:
        return self.stream(run_id=run_id, workspace_id=workspace_id)

    async def _execute(self, record: RuntimeRunRecord) -> RuntimeHandle:
        try:
            result = await (self.graph.ainvoke(record.state) if record.execution_mode == "langgraph"
                            else self.legacy_adapter.run(record.request, record.state))
        except Exception as exc:
            if record.execution_mode == "langgraph" and self.flags.allow_legacy_fallback and not record.state.side_effects_started:
                self._emit(record, RuntimeTransition(event_type="runtime.fallback", payload={"reason": str(exc)}))
                result = await self.legacy_adapter.run(record.request, record.state)
            else:
                failed = self._state_with(record.state, status=RuntimeStatus.FAILED)
                record = RuntimeRunRecord(record.request, failed, record.execution_mode, record.created_at)
                self.runs.save(record)
                self._emit(record, RuntimeTransition(event_type="run.failed", payload={"category": classify_error(exc).value, "message": str(exc)}))
                return self._handle(record)
        record = RuntimeRunRecord(record.request, result.state, result.engine, record.created_at)
        self.runs.save(record)
        for transition in result.state.transitions:
            self._emit(record, transition)
        return self._handle(record)

    def _emit(self, record: RuntimeRunRecord, transition: RuntimeTransition) -> RuntimeEvent:
        return self.events.append(run_id=record.state.identity.run_id, workspace_id=record.state.identity.workspace_id, transition=transition)

    @staticmethod
    def _handle(record: RuntimeRunRecord) -> RuntimeHandle:
        return RuntimeHandle(run_id=record.state.identity.run_id, thread_id=record.state.identity.thread_id,
                             status=record.state.status, execution_mode=record.execution_mode, created_at=record.created_at)

    @staticmethod
    def _assert_actor(record: RuntimeRunRecord, user_id: str) -> None:
        if record.state.identity.user_id != user_id:
            raise RuntimeAccessDenied()

    @staticmethod
    def _state_with(state: AtlasAgentState, **changes: object) -> AtlasAgentState:
        data = state.model_dump()
        data.update(changes)
        return AtlasAgentState.model_validate(data)
