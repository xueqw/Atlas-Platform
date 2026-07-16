"""Event-ledger contracts with an in-memory implementation for local tests."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from threading import RLock
from typing import Protocol
import json
import uuid

from .runtime_contract import RuntimeAccessDenied, RuntimeEvent, RuntimeTransition, utcnow


class RuntimeEventRepository(Protocol):
    """DB implementations must allocate `sequence` and insert in one transaction."""

    def append(self, *, run_id: str, workspace_id: str, transition: RuntimeTransition,
               event_id: str | None = None, timestamp: datetime | None = None) -> RuntimeEvent: ...

    def replay(self, *, run_id: str, workspace_id: str, after_sequence: int = 0) -> tuple[RuntimeEvent, ...]: ...


class InMemoryRuntimeEventRepository:
    """Thread-safe reference implementation; storage is JSON round-tripped on every boundary."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._events: dict[str, list[str]] = defaultdict(list)
        self._workspace_by_run: dict[str, str] = {}

    def append(self, *, run_id: str, workspace_id: str, transition: RuntimeTransition,
               event_id: str | None = None, timestamp: datetime | None = None) -> RuntimeEvent:
        with self._lock:
            owner = self._workspace_by_run.setdefault(run_id, workspace_id)
            if owner != workspace_id:
                raise RuntimeAccessDenied()
            event = RuntimeEvent(
                event_id=event_id or str(uuid.uuid4()),
                run_id=run_id,
                workspace_id=workspace_id,
                sequence=len(self._events[run_id]) + 1,
                timestamp=timestamp or utcnow(),
                type=transition.event_type,
                payload=transition.payload,
            )
            self._events[run_id].append(event.model_dump_json())
            return RuntimeEvent.model_validate_json(self._events[run_id][-1])

    def replay(self, *, run_id: str, workspace_id: str, after_sequence: int = 0) -> tuple[RuntimeEvent, ...]:
        if after_sequence < 0:
            raise ValueError("after_sequence must be non-negative")
        with self._lock:
            owner = self._workspace_by_run.get(run_id)
            if owner is None or owner != workspace_id:
                raise RuntimeAccessDenied()
            return tuple(
                RuntimeEvent.model_validate_json(serialized)
                for serialized in self._events[run_id]
                if json.loads(serialized)["sequence"] > after_sequence
            )
