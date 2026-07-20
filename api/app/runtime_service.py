"""Feature-flagged Runtime service with in-memory repositories for Phase 1."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from threading import RLock
from typing import Awaitable, Callable, Protocol
import os
import uuid

from .runtime_contract import (
    RuntimeAccessDenied,
    RuntimeEvent,
    RuntimeHandle,
    RuntimeInterruptCreate,
    RuntimeInterruptRecord,
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


class RuntimeInterruptRepository(Protocol):
    def create(self, request: RuntimeInterruptCreate) -> RuntimeInterruptRecord: ...
    def resolve(self, request: RuntimeResumeRequest) -> RuntimeInterruptRecord: ...
    def get_by_nonce(
        self, *, run_id: str, workspace_id: str, user_id: str, nonce: str,
    ) -> RuntimeInterruptRecord | None: ...


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
            state = AtlasAgentState.initial(
                request.make_identity(run_id), request.input,
                requested_execution_strategy=request.execution_strategy.value,
                requested_resources=request.requested_resources,
            )
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


class InMemoryRuntimeInterruptRepository:
    """Atomic reference store used by unit tests; production uses SQL row locks."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._records: dict[str, RuntimeInterruptRecord] = {}
        self._nonces: set[tuple[str, str]] = set()

    def create(self, request: RuntimeInterruptCreate) -> RuntimeInterruptRecord:
        with self._lock:
            key = (request.run_id, request.nonce)
            if key in self._nonces:
                raise ValueError("interrupt nonce was already used for this run")
            if request.expires_at <= utcnow():
                raise ValueError("interrupt expiry must be in the future")
            record = RuntimeInterruptRecord(
                interrupt_id=str(uuid.uuid4()),
                run_id=request.run_id,
                workspace_id=request.workspace_id,
                user_id=request.user_id,
                status="pending",
                parameter_digest=request.parameter_digest,
                resource_version=request.resource_version,
                scope=request.scope,
                nonce=request.nonce,
                expires_at=request.expires_at,
                payload=request.payload,
            )
            self._records[record.interrupt_id] = record
            self._nonces.add(key)
            return record

    def resolve(self, request: RuntimeResumeRequest) -> RuntimeInterruptRecord:
        with self._lock:
            record = self._records.get(request.interrupt_id or "")
            if record is None or (
                record.run_id != request.run_id
                or record.workspace_id != request.workspace_id
                or record.user_id != request.user_id
            ):
                raise RuntimeAccessDenied()
            if record.status != "pending":
                raise ValueError("interrupt decision was already consumed")
            if record.expires_at <= utcnow():
                expired = record.model_copy(update={"status": "expired", "resolved_at": utcnow()})
                self._records[record.interrupt_id] = expired
                raise ValueError("interrupt decision has expired")
            if (
                record.nonce != request.nonce
                or record.parameter_digest != request.parameter_digest
                or record.resource_version != request.resource_version
            ):
                raise RuntimeAccessDenied()
            resolved = record.model_copy(update={
                "status": "approved" if request.decision == "approve" else "denied",
                "resolved_at": utcnow(),
            })
            self._records[record.interrupt_id] = resolved
            return resolved

    def get_by_nonce(
        self, *, run_id: str, workspace_id: str, user_id: str, nonce: str,
    ) -> RuntimeInterruptRecord | None:
        with self._lock:
            return next((
                record for record in self._records.values()
                if record.run_id == run_id
                and record.workspace_id == workspace_id
                and record.user_id == user_id
                and record.nonce == nonce
            ), None)


class RuntimeExecutionSupervisor:
    """Keep strong references to background runs and make cancellation observable."""

    def __init__(self) -> None:
        self._tasks: dict[tuple[str, str], asyncio.Task[RuntimeHandle]] = {}

    def submit(self, key: tuple[str, str], factory: Callable[[], Awaitable[RuntimeHandle]]) -> bool:
        existing = self._tasks.get(key)
        if existing is not None and not existing.done():
            return False
        task = asyncio.create_task(factory(), name=f"atlas-runtime:{key[0]}:{key[1]}")
        self._tasks[key] = task
        task.add_done_callback(lambda completed, task_key=key: self._discard(task_key, completed))
        return True

    def cancel(self, key: tuple[str, str]) -> None:
        task = self._tasks.get(key)
        if task is not None and not task.done():
            task.cancel()

    async def wait(self, key: tuple[str, str]) -> None:
        task = self._tasks.get(key)
        if task is not None:
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                pass

    def is_running(self, key: tuple[str, str]) -> bool:
        task = self._tasks.get(key)
        return task is not None and not task.done()

    def _discard(self, key: tuple[str, str], task: asyncio.Task[RuntimeHandle]) -> None:
        if self._tasks.get(key) is task:
            self._tasks.pop(key, None)
        if not task.cancelled():
            task.exception()  # retrieve unexpected failures; _execute normally contains them


_RUNTIME_SUPERVISOR = RuntimeExecutionSupervisor()


class AgentRuntimeService:
    """Public Phase 1 entry point. All client-visible state moves through Runtime Events."""

    def __init__(self, graph: RuntimeRunner, *, event_repository: RuntimeEventRepository | None = None,
                 run_repository: RuntimeRunRepository | None = None, legacy_adapter: LegacyRuntimeAdapter | None = None,
                 interrupt_repository: RuntimeInterruptRepository | None = None,
                 supervisor: RuntimeExecutionSupervisor | None = None,
                 flags: RuntimeFeatureFlags | None = None) -> None:
        self.graph = graph
        self.events = event_repository or InMemoryRuntimeEventRepository()
        self.runs = run_repository or InMemoryRuntimeRunRepository()
        self.interrupts = interrupt_repository or InMemoryRuntimeInterruptRepository()
        self.legacy_adapter = legacy_adapter or GraphLegacyAdapter()
        self.supervisor = supervisor or _RUNTIME_SUPERVISOR
        self.flags = flags or RuntimeFeatureFlags.from_environment()

    async def start(self, request: RuntimeStartRequest) -> RuntimeHandle:
        mode = "langgraph" if self.flags.langgraph_enabled else "legacy"
        record, created = self.runs.create_or_get(request, mode)
        if not created:
            return self._handle(record)
        record = RuntimeRunRecord(
            record.request,
            self._state_with(record.state, status=RuntimeStatus.RUNNING),
            record.execution_mode,
            record.created_at,
        )
        self.runs.save(record)
        self._emit(record, RuntimeTransition(event_type="run.started", payload={"mode": mode}))
        self.supervisor.submit(self._task_key(record), lambda: self._execute(record))
        return self._handle(record)

    def stream(self, *, run_id: str, workspace_id: str, after_sequence: int = 0) -> tuple[RuntimeEvent, ...]:
        return self.events.replay(run_id=run_id, workspace_id=workspace_id, after_sequence=after_sequence)

    async def resume(self, request: RuntimeResumeRequest) -> RuntimeHandle:
        record = self.runs.get(run_id=request.run_id, workspace_id=request.workspace_id)
        self._assert_actor(record, request.user_id)
        missing_worker_interrupt = (
            record.state.status is RuntimeStatus.PAUSED
            and bool(record.state.complex_context.get("authorization_requests"))
            and not record.state.complex_context.get("runtime_interrupt_id")
        )
        if missing_worker_interrupt:
            # Crash compensation: graph pause and parent interrupt persistence
            # are separate durable boundaries. Recreate the actor-bound gate
            # before accepting any resume; the caller must retry with the new
            # exact binding emitted by interrupt.requested.
            self._ensure_adaptive_worker_interrupt(record)
            raise ValueError(
                "worker write authorization was recovered; retry with the emitted bound interrupt"
            )
        escalation = (
            record.state.review_result.get("verdict") == "ESCALATE"
            and record.state.status is RuntimeStatus.PAUSED
        )
        if escalation and request.escalation_decision is None:
            raise ValueError("escalation resume requires an explicit approve or deny decision")
        if escalation and request.escalation_decision == "deny":
            cancelled = RuntimeRunRecord(
                record.request,
                self._state_with(record.state, status=RuntimeStatus.CANCELLED),
                record.execution_mode,
                record.created_at,
            )
            self.runs.save(cancelled)
            self._emit(cancelled, RuntimeTransition(
                event_type="run.cancelled", payload={"reason": "escalation denied"},
            ))
            return self._handle(cancelled)
        if escalation:
            record = RuntimeRunRecord(
                record.request,
                self._state_with(
                    record.state,
                    complex_context={
                        **record.state.complex_context,
                        "escalation_approved": True,
                    },
                ),
                record.execution_mode,
                record.created_at,
            )
            self.runs.save(record)
            self._emit(record, RuntimeTransition(
                event_type="escalation.resolved", payload={"decision": "approved"},
            ))
        pending_worker_interrupt = (
            record.state.status is RuntimeStatus.PAUSED
            and bool(record.state.complex_context.get("runtime_interrupt_id"))
        )
        if pending_worker_interrupt and request.interrupt_id is None:
            raise ValueError("worker write resume requires an explicit bound interrupt decision")
        if pending_worker_interrupt and request.interrupt_id != record.state.complex_context.get("runtime_interrupt_id"):
            raise RuntimeAccessDenied()
        resolution = None
        recovered_resolution = False
        if request.interrupt_id:
            try:
                resolution = self.interrupts.resolve(request)
            except ValueError:
                existing = self.interrupts.get_by_nonce(
                    run_id=request.run_id,
                    workspace_id=request.workspace_id,
                    user_id=request.user_id,
                    nonce=request.nonce or "",
                )
                expected_status = "approved" if request.decision == "approve" else "denied"
                if not (
                    pending_worker_interrupt
                    and existing is not None
                    and existing.interrupt_id == request.interrupt_id
                    and existing.status == expected_status
                    and existing.parameter_digest == request.parameter_digest
                    and existing.resource_version == request.resource_version
                    and existing.nonce == request.nonce
                ):
                    raise
                # The decision commit won, but its parent Runtime projection
                # did not. Reapply only while that parent still advertises the
                # same paused interrupt; after graph progress this path closes
                # and normal anti-replay remains authoritative.
                resolution = existing
                recovered_resolution = True
        if resolution is not None:
            self._emit(record, RuntimeTransition(
                event_type="interrupt.resolved",
                payload={
                    "interrupt_id": resolution.interrupt_id,
                    "decision": resolution.status,
                    "recovered": recovered_resolution,
                },
            ))
            if resolution.status == "denied":
                cancelled = RuntimeRunRecord(
                    record.request,
                    self._state_with(record.state, status=RuntimeStatus.CANCELLED),
                    record.execution_mode,
                    record.created_at,
                )
                self.runs.save(cancelled)
                self._emit(cancelled, RuntimeTransition(event_type="run.cancelled", payload={"reason": "authorization denied"}))
                return self._handle(cancelled)
            if resolution.payload.get("kind") == "adaptive_worker_authorization":
                requests = resolution.payload.get("authorization_requests") or []
                authorizations = {
                    str(item["logical_task_id"]): {
                        "orchestration_run_id": str(item["orchestration_run_id"]),
                        "physical_task_id": str(item["physical_task_id"]),
                        "ticket_id": str(item["ticket_id"]),
                        "nonce": str(item["nonce"]),
                        "resource_version": str(item["resource_version"]),
                    }
                    for item in requests
                    if isinstance(item, dict)
                }
                record = RuntimeRunRecord(
                    record.request,
                    self._state_with(
                        record.state,
                        complex_context={
                            **record.state.complex_context,
                            "worker_authorizations": authorizations,
                            "worker_authorization_approved": True,
                            "worker_authorization_applied": True,
                        },
                    ),
                    record.execution_mode,
                    record.created_at,
                )
                self.runs.save(record)
        if record.state.status in {RuntimeStatus.SUCCEEDED, RuntimeStatus.FAILED, RuntimeStatus.CANCELLED}:
            return self._handle(record)
        if record.state.status is RuntimeStatus.RUNNING and self.supervisor.is_running(self._task_key(record)):
            return self._handle(record)
        if record.state.status is RuntimeStatus.PAUSED:
            await self.supervisor.wait(self._task_key(record))
        resumed = RuntimeRunRecord(record.request, self._state_with(record.state, status=RuntimeStatus.RUNNING), record.execution_mode, record.created_at)
        self.runs.save(resumed)
        self._emit(resumed, RuntimeTransition(event_type="run.resumed", payload={}))
        self.supervisor.submit(
            self._task_key(resumed),
            lambda: self._execute(resumed, resume=resolution is not None or record.state.status is RuntimeStatus.PAUSED),
        )
        return self._handle(resumed)

    def request_interrupt(self, request: RuntimeInterruptCreate) -> RuntimeInterruptRecord:
        record = self.runs.get(run_id=request.run_id, workspace_id=request.workspace_id)
        self._assert_actor(record, request.user_id)
        if record.state.status in {RuntimeStatus.SUCCEEDED, RuntimeStatus.FAILED, RuntimeStatus.CANCELLED}:
            raise ValueError("terminal runs cannot be interrupted")
        interrupt = self.interrupts.create(request)
        self.supervisor.cancel(self._task_key(record))
        paused = RuntimeRunRecord(
            record.request,
            self._state_with(record.state, status=RuntimeStatus.PAUSED),
            record.execution_mode,
            record.created_at,
        )
        self.runs.save(paused)
        self._emit(paused, RuntimeTransition(event_type="interrupt.requested", payload={
            "interrupt_id": interrupt.interrupt_id,
            "nonce": interrupt.nonce,
            "parameter_digest": interrupt.parameter_digest,
            "resource_version": interrupt.resource_version,
            "scope": list(interrupt.scope),
            "expires_at": interrupt.expires_at.isoformat(),
        }))
        return interrupt

    def cancel(self, *, run_id: str, workspace_id: str, user_id: str) -> RuntimeHandle:
        record = self.runs.get(run_id=run_id, workspace_id=workspace_id)
        self._assert_actor(record, user_id)
        if record.state.status not in {RuntimeStatus.SUCCEEDED, RuntimeStatus.FAILED, RuntimeStatus.CANCELLED}:
            self.supervisor.cancel(self._task_key(record))
            record = RuntimeRunRecord(record.request, self._state_with(record.state, status=RuntimeStatus.CANCELLED), record.execution_mode, record.created_at)
            self.runs.save(record)
            self._emit(record, RuntimeTransition(event_type="run.cancelled", payload={}))
        return self._handle(record)

    def get_state(self, *, run_id: str, workspace_id: str) -> RuntimeState:
        return self.runs.get(run_id=run_id, workspace_id=workspace_id).state.to_runtime_state()

    def get_history(self, *, run_id: str, workspace_id: str) -> tuple[RuntimeEvent, ...]:
        return self.stream(run_id=run_id, workspace_id=workspace_id)

    async def wait(self, *, run_id: str, workspace_id: str) -> RuntimeHandle:
        record = self.runs.get(run_id=run_id, workspace_id=workspace_id)
        await self.supervisor.wait(self._task_key(record))
        return self._handle(self.runs.get(run_id=run_id, workspace_id=workspace_id))

    async def _execute(self, record: RuntimeRunRecord, *, resume: bool = False) -> RuntimeHandle:
        try:
            if record.execution_mode == "langgraph":
                record = await self._execute_graph(record, resume=resume)
                record = self._ensure_adaptive_worker_interrupt(record)
                return self._handle(record)
            result = await self.legacy_adapter.run(record.request, record.state)
        except Exception as exc:
            # A streaming graph may have persisted a side-effect boundary before
            # a later iteration raises. Reload the durable snapshot; the stale
            # record captured at task start must never authorize legacy replay.
            latest = self.runs.get(
                run_id=record.state.identity.run_id,
                workspace_id=record.state.identity.workspace_id,
            )
            if latest.execution_mode == "langgraph" and self.flags.allow_legacy_fallback and not latest.state.side_effects_started:
                self._emit(latest, RuntimeTransition(event_type="runtime.fallback", payload={"reason": str(exc)}))
                result = await self.legacy_adapter.run(latest.request, latest.state)
                record = latest
            else:
                failed = self._state_with(latest.state, status=RuntimeStatus.FAILED)
                record = RuntimeRunRecord(latest.request, failed, latest.execution_mode, latest.created_at)
                self.runs.save(record)
                self._emit(record, RuntimeTransition(event_type="run.failed", payload={"category": classify_error(exc).value, "message": str(exc)}))
                return self._handle(record)
        record = RuntimeRunRecord(record.request, result.state, result.engine, record.created_at)
        latest = self.runs.get(run_id=record.state.identity.run_id, workspace_id=record.state.identity.workspace_id)
        if latest.state.status is RuntimeStatus.CANCELLED:
            return self._handle(latest)
        self.runs.save(record)
        for transition in result.state.transitions[len(latest.state.transitions):]:
            self._emit(record, transition)
        return self._handle(record)

    async def _execute_graph(self, record: RuntimeRunRecord, *, resume: bool) -> RuntimeRunRecord:
        stream_method = getattr(self.graph, "aresume_stream" if resume else "astream", None)
        if stream_method is None:
            invoke = getattr(self.graph, "aresume", None) if resume else None
            result = await (invoke(record.state) if invoke is not None else self.graph.ainvoke(record.state))
            return self._persist_graph_snapshot(record, result)

        current = record
        async for result in stream_method(record.state):
            current = self._persist_graph_snapshot(current, result)
            if current.state.status is RuntimeStatus.CANCELLED:
                break
        return current

    def _ensure_adaptive_worker_interrupt(self, record: RuntimeRunRecord) -> RuntimeRunRecord:
        """Create the actor-bound parent gate for paused child write tickets."""
        context = record.state.complex_context
        requests = context.get("authorization_requests") or []
        if (
            record.state.status is not RuntimeStatus.PAUSED
            or not requests
            or context.get("runtime_interrupt_id")
        ):
            return record
        if not all(isinstance(item, dict) for item in requests):
            raise ValueError("adaptive authorization requests must be objects")
        canonical = json.dumps(requests, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        parameter_digest = hashlib.sha256(canonical.encode()).hexdigest()
        resource_versions = {str(item.get("resource_version") or "") for item in requests}
        if len(resource_versions) != 1 or "" in resource_versions:
            raise ValueError("adaptive authorization requests require one resource version")
        expiries = [datetime.fromisoformat(str(item.get("expires_at") or "")) for item in requests]
        recovery_nonce = hashlib.sha256(
            f"adaptive-worker-authorization:{record.state.identity.run_id}:{parameter_digest}:"
            f"{next(iter(resource_versions))}".encode()
        ).hexdigest()
        interrupt = self.interrupts.get_by_nonce(
            run_id=record.state.identity.run_id,
            workspace_id=record.state.identity.workspace_id,
            user_id=record.state.identity.user_id,
            nonce=recovery_nonce,
        )
        if interrupt is None:
            interrupt = self.interrupts.create(RuntimeInterruptCreate(
                run_id=record.state.identity.run_id,
                workspace_id=record.state.identity.workspace_id,
                user_id=record.state.identity.user_id,
                parameter_digest=parameter_digest,
                resource_version=next(iter(resource_versions)),
                scope=tuple(sorted({
                    scope for item in requests for scope in item.get("scope") or ()
                })),
                nonce=recovery_nonce,
                expires_at=min(expiries),
                payload={
                    "kind": "adaptive_worker_authorization",
                    "authorization_requests": requests,
                },
            ))
        updated = RuntimeRunRecord(
            record.request,
            self._state_with(
                record.state,
                complex_context={
                    **context,
                    "runtime_interrupt_id": interrupt.interrupt_id,
                },
            ),
            record.execution_mode,
            record.created_at,
        )
        self.runs.save(updated)
        self._emit(updated, RuntimeTransition(event_type="interrupt.requested", payload={
            "interrupt_id": interrupt.interrupt_id,
            "nonce": interrupt.nonce,
            "parameter_digest": interrupt.parameter_digest,
            "resource_version": interrupt.resource_version,
            "scope": list(interrupt.scope),
            "expires_at": interrupt.expires_at.isoformat(),
            "reason": "worker_authorization_required",
            "authorization_requests": [
                {
                    "logical_task_id": item.get("logical_task_id"),
                    "worker_id": item.get("worker_id"),
                    "tool_name": item.get("tool_name"),
                    "parameters": item.get("parameters") or {},
                    "scope": item.get("scope") or [],
                }
                for item in requests
            ],
        }))
        return updated

    def _persist_graph_snapshot(self, previous: RuntimeRunRecord, result: GraphExecutionResult) -> RuntimeRunRecord:
        latest = self.runs.get(
            run_id=previous.state.identity.run_id,
            workspace_id=previous.state.identity.workspace_id,
        )
        if latest.state.status is RuntimeStatus.CANCELLED:
            return latest
        # The selected service path is durable run metadata. A graph may use a
        # manual compatibility engine internally, but persisting that detail as
        # the run mode would route resume into the legacy adapter and bypass the
        # graph's authorization/recovery logic.
        current = RuntimeRunRecord(
            previous.request, result.state, previous.execution_mode, previous.created_at,
        )
        self.runs.save(current)
        for transition in result.state.transitions[len(previous.state.transitions):]:
            self._emit(current, transition)
        return current

    def _emit(self, record: RuntimeRunRecord, transition: RuntimeTransition) -> RuntimeEvent:
        return self.events.append(run_id=record.state.identity.run_id, workspace_id=record.state.identity.workspace_id, transition=transition)

    @staticmethod
    def _handle(record: RuntimeRunRecord) -> RuntimeHandle:
        return RuntimeHandle(run_id=record.state.identity.run_id, thread_id=record.state.identity.thread_id,
                             status=record.state.status, execution_mode=record.execution_mode,
                             execution_strategy=(record.state.execution_strategy
                                                 or record.request.execution_strategy.value),
                             created_at=record.created_at)

    @staticmethod
    def _assert_actor(record: RuntimeRunRecord, user_id: str) -> None:
        if record.state.identity.user_id != user_id:
            raise RuntimeAccessDenied()

    @staticmethod
    def _task_key(record: RuntimeRunRecord) -> tuple[str, str]:
        return record.state.identity.workspace_id, record.state.identity.run_id

    @staticmethod
    def _state_with(state: AtlasAgentState, **changes: object) -> AtlasAgentState:
        data = state.model_dump()
        data.update(changes)
        return AtlasAgentState.model_validate(data)
