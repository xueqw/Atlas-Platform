"""Redis-hot/PostgreSQL-durable Session Context storage."""
from __future__ import annotations

import json
from typing import Any, Callable, Protocol

from sqlalchemy.orm import Session

from .memory_ledger import MemoryScope, checkpoint_state, load_checkpoint, rebuild_checkpoint


class RedisSessionClient(Protocol):
    def get(self, key: str) -> bytes | str | None: ...
    def setex(self, key: str, ttl: int, value: str) -> Any: ...
    def delete(self, key: str) -> Any: ...


class DurableSessionContextStore:
    """Treat Redis as a cache and the ledger/checkpoint as the source of truth."""

    def __init__(self, session_factory: Callable[[], Session], redis: RedisSessionClient,
                 *, ttl_seconds: int = 3600):
        self.session_factory = session_factory
        self.redis = redis
        self.ttl_seconds = ttl_seconds

    @staticmethod
    def _key(scope: MemoryScope, session_id: str) -> str:
        return ":".join(("atlas", "memory", "session", scope.workspace_id, scope.user_id,
                         scope.agent_id, scope.run_id or "-", scope.worker_id or "-", session_id))

    def write(self, *, scope: MemoryScope, session_id: str, snapshot: dict[str, Any],
              expected_version: int, idempotency_key: str) -> dict[str, Any]:
        with self.session_factory() as db:
            checkpoint, _ = checkpoint_state(
                db, scope=scope, state_kind="session", state_key=session_id,
                snapshot=snapshot, expected_version=expected_version,
                idempotency_key=idempotency_key,
            )
            db.commit()
            durable = {"version": checkpoint.version, "snapshot": dict(checkpoint.snapshot)}
        try:
            self.redis.setex(self._key(scope, session_id), self.ttl_seconds,
                             json.dumps(durable, ensure_ascii=False, sort_keys=True))
        except Exception:
            # Redis loss cannot roll back the committed durable state.
            pass
        return durable

    def read(self, *, scope: MemoryScope, session_id: str) -> dict[str, Any] | None:
        try:
            cached = self.redis.get(self._key(scope, session_id))
            if cached is not None:
                decoded = cached.decode() if isinstance(cached, bytes) else cached
                value = json.loads(decoded)
                if isinstance(value, dict) and isinstance(value.get("snapshot"), dict):
                    return value
        except Exception:
            pass
        return self.rebuild(scope=scope, session_id=session_id)

    def rebuild(self, *, scope: MemoryScope, session_id: str) -> dict[str, Any] | None:
        with self.session_factory() as db:
            checkpoint = load_checkpoint(
                db, scope=scope, state_kind="session", state_key=session_id
            )
            value = ({"version": checkpoint.version, "snapshot": dict(checkpoint.snapshot)}
                     if checkpoint is not None else rebuild_checkpoint(
                         db, scope=scope, state_kind="session", state_key=session_id))
        if value is not None:
            try:
                self.redis.setex(self._key(scope, session_id), self.ttl_seconds,
                                 json.dumps(value, ensure_ascii=False, sort_keys=True))
            except Exception:
                pass
        return value
