"""Redis client facade with a transparent in-process fallback (Batch C).

A single entry point the rest of the backend calls for short-term cache, locks
and lightweight job queues. The concrete backend is chosen ONCE at import time:

  - ``ENABLE_REDIS=true`` and the server pings → :class:`_RedisBackend`.
  - otherwise (disabled, ``redis`` not installed, or ping fails) →
    :class:`_InProcessBackend`, logging a WARNING in the configured-but-unreachable
    case.

Callers never branch on ``ENABLE_REDIS``: the facade exposes the same methods on
both backends, so disabling Redis is byte-for-byte the previous in-process
behaviour (single-process semantics). Every Redis key is namespaced with
``REDIS_PREFIX`` so one Redis instance can host multiple tenants safely.

A single runtime call that raises is swallowed and degraded — Redis must never
take down the main request path.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from collections import deque
from typing import Any, Deque, Dict, Optional, Tuple

# Importing config loads backend/.env into os.environ (idempotent), so the env
# flags below resolve the same whether imported by the app or a test harness.
from app.core import config as _config  # noqa: F401

_log = logging.getLogger(__name__)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


_PREFIX = (os.environ.get("REDIS_PREFIX") or "agentgw").strip()


def _key(name: str) -> str:
    """Namespace a logical key under REDIS_PREFIX."""
    return f"{_PREFIX}:{name}"


class _InProcessBackend:
    """Single-process fallback: dict cache (with expiry) + threading locks +
    in-memory deques. Equivalent to the pre-Redis behaviour."""

    def __init__(self) -> None:
        self.name = "inprocess"
        self._cache: Dict[str, Tuple[Any, Optional[float]]] = {}
        self._locks: Dict[str, Tuple[str, float]] = {}  # name -> (token, expires_at)
        self._queues: Dict[str, Deque[str]] = {}
        self._guard = threading.RLock()

    # — cache —
    def get_cache(self, key: str) -> Optional[Any]:
        with self._guard:
            item = self._cache.get(key)
            if item is None:
                return None
            value, expires = item
            if expires is not None and time.time() >= expires:
                self._cache.pop(key, None)
                return None
            return value

    def set_cache(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        with self._guard:
            expires = time.time() + ttl if ttl else None
            self._cache[key] = (value, expires)

    def delete_cache(self, key: str) -> None:
        with self._guard:
            self._cache.pop(key, None)

    # — locks (SETNX-with-TTL semantics) —
    def acquire_lock(self, name: str, ttl: int = 60) -> Optional[str]:
        with self._guard:
            existing = self._locks.get(name)
            now = time.time()
            if existing is not None and existing[1] > now:
                return None  # held and not expired
            token = uuid.uuid4().hex
            self._locks[name] = (token, now + ttl)
            return token

    def release_lock(self, name: str, token: str) -> bool:
        with self._guard:
            existing = self._locks.get(name)
            if existing is not None and existing[0] == token:
                self._locks.pop(name, None)
                return True
            return False

    # — queue —
    def queue_push(self, queue: str, item: str) -> None:
        with self._guard:
            self._queues.setdefault(queue, deque()).append(item)

    def queue_pop(self, queue: str) -> Optional[str]:
        with self._guard:
            q = self._queues.get(queue)
            if not q:
                return None
            return q.popleft()

    def queue_ack(self, queue: str, item: str) -> None:
        # In-process queue has no in-flight set; pop already removed it.
        return None

    def health_check(self) -> bool:
        return True


# Lua: delete a key only if its value equals the caller's token (safe release).
_RELEASE_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "return redis.call('del', KEYS[1]) else return 0 end"
)


class _RedisBackend:
    """Real Redis backend. Keys are already namespaced by the facade."""

    def __init__(self, client: Any) -> None:
        self.name = "redis"
        self._r = client
        try:
            self._release = client.register_script(_RELEASE_LUA)
        except Exception:
            self._release = None

    def get_cache(self, key: str) -> Optional[Any]:
        val = self._r.get(key)
        return val.decode() if isinstance(val, (bytes, bytearray)) else val

    def set_cache(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        self._r.set(key, value, ex=ttl)

    def delete_cache(self, key: str) -> None:
        self._r.delete(key)

    def acquire_lock(self, name: str, ttl: int = 60) -> Optional[str]:
        token = uuid.uuid4().hex
        ok = self._r.set(name, token, nx=True, ex=ttl)
        return token if ok else None

    def release_lock(self, name: str, token: str) -> bool:
        if self._release is not None:
            try:
                return bool(self._release(keys=[name], args=[token]))
            except Exception:
                pass
        # Fallback CAS without Lua (best-effort; small race window).
        cur = self._r.get(name)
        cur = cur.decode() if isinstance(cur, (bytes, bytearray)) else cur
        if cur == token:
            self._r.delete(name)
            return True
        return False

    def queue_push(self, queue: str, item: str) -> None:
        self._r.lpush(queue, item)

    def queue_pop(self, queue: str) -> Optional[str]:
        val = self._r.rpop(queue)
        return val.decode() if isinstance(val, (bytes, bytearray)) else val

    def queue_ack(self, queue: str, item: str) -> None:
        # rpop already removed the item; no separate in-flight set in this phase.
        return None

    def health_check(self) -> bool:
        try:
            return bool(self._r.ping())
        except Exception:
            return False


def _build_backend():
    """Pick the backend once: Redis when enabled AND reachable, else in-process."""
    if not _env_bool("ENABLE_REDIS", False):
        return _InProcessBackend()
    url = (os.environ.get("REDIS_URL") or "").strip()
    if not url:
        _log.warning("ENABLE_REDIS=true but REDIS_URL is empty; falling back to in-process backend")
        return _InProcessBackend()
    try:
        import redis  # lazy: only needed when Redis is actually enabled

        client = redis.Redis.from_url(url)
        if not client.ping():
            raise RuntimeError("ping returned falsy")
        _log.info("redis_client: connected to %s (prefix=%s)", url, _PREFIX)
        return _RedisBackend(client)
    except Exception as exc:
        _log.warning(
            "redis_client: Redis unavailable (%s: %s); falling back to in-process backend",
            type(exc).__name__, exc,
        )
        return _InProcessBackend()


# Module-level singleton backend (chosen once at import).
_backend = _build_backend()


def _set_backend_for_test(backend) -> None:
    """Test seam: swap the active backend (e.g. in-process or fakeredis)."""
    global _backend
    _backend = backend


def backend_name() -> str:
    return _backend.name


# ── Facade: callers use these, never the backends directly ──────────────────

def get_cache(name: str) -> Optional[Any]:
    try:
        return _backend.get_cache(_key(name))
    except Exception as exc:
        _log.warning("get_cache(%s) failed (%s); returning None", name, exc)
        return None


def set_cache(name: str, value: Any, ttl: Optional[int] = None) -> None:
    try:
        _backend.set_cache(_key(name), value, ttl=ttl)
    except Exception as exc:
        _log.warning("set_cache(%s) failed (%s)", name, exc)


def delete_cache(name: str) -> None:
    try:
        _backend.delete_cache(_key(name))
    except Exception as exc:
        _log.warning("delete_cache(%s) failed (%s)", name, exc)


def acquire_lock(name: str, ttl: int = 60) -> Optional[str]:
    try:
        return _backend.acquire_lock(_key(f"lock:{name}"), ttl=ttl)
    except Exception as exc:
        _log.warning("acquire_lock(%s) failed (%s); treating as unlocked", name, exc)
        return None


def release_lock(name: str, token: str) -> bool:
    if not token:
        return False
    try:
        return _backend.release_lock(_key(f"lock:{name}"), token)
    except Exception as exc:
        _log.warning("release_lock(%s) failed (%s)", name, exc)
        return False


def queue_push(queue: str, item: str) -> None:
    try:
        _backend.queue_push(_key(f"queue:{queue}"), item)
    except Exception as exc:
        _log.warning("queue_push(%s) failed (%s)", queue, exc)


def queue_pop(queue: str) -> Optional[str]:
    try:
        return _backend.queue_pop(_key(f"queue:{queue}"))
    except Exception as exc:
        _log.warning("queue_pop(%s) failed (%s)", queue, exc)
        return None


def queue_ack(queue: str, item: str) -> None:
    try:
        _backend.queue_ack(_key(f"queue:{queue}"), item)
    except Exception as exc:
        _log.warning("queue_ack(%s) failed (%s)", queue, exc)


def health_check() -> bool:
    try:
        return _backend.health_check()
    except Exception:
        return False
