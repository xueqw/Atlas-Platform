from __future__ import annotations

import json
import threading
import time
from typing import Any

import redis

from .config import settings


class StateStore:
    """TTL JSON state with a local test fallback and a fail-closed production mode."""

    def __init__(self) -> None:
        self._redis = redis.Redis.from_url(settings.redis_url, decode_responses=True)
        self._local: dict[str, tuple[float, str]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(namespace: str, key: str) -> str:
        return f"atlas:{namespace}:{key}"

    def _fallback_allowed(self) -> bool:
        return not settings.redis_required and settings.environment != "production"

    def set(self, namespace: str, key: str, value: Any, ttl: int) -> None:
        encoded = json.dumps(value, ensure_ascii=False)
        try:
            self._redis.setex(self._key(namespace, key), ttl, encoded)
        except redis.RedisError:
            if not self._fallback_allowed():
                raise RuntimeError("Redis unavailable")
            with self._lock:
                self._local[self._key(namespace, key)] = (time.time() + ttl, encoded)

    def get(self, namespace: str, key: str, *, consume: bool = False) -> Any | None:
        redis_key = self._key(namespace, key)
        try:
            raw = self._redis.getdel(redis_key) if consume else self._redis.get(redis_key)
        except redis.RedisError:
            if not self._fallback_allowed():
                raise RuntimeError("Redis unavailable")
            with self._lock:
                item = self._local.pop(redis_key, None) if consume else self._local.get(redis_key)
                if not item or item[0] <= time.time():
                    self._local.pop(redis_key, None)
                    return None
                raw = item[1]
        return json.loads(raw) if raw else None

    def delete(self, namespace: str, key: str) -> None:
        redis_key = self._key(namespace, key)
        try:
            self._redis.delete(redis_key)
        except redis.RedisError:
            if not self._fallback_allowed():
                raise RuntimeError("Redis unavailable")
            with self._lock:
                self._local.pop(redis_key, None)

    def delete_prefix(self, namespace: str, prefix: str) -> None:
        redis_prefix = self._key(namespace, prefix)
        try:
            keys = list(self._redis.scan_iter(match=redis_prefix + "*", count=200))
            if keys:
                self._redis.delete(*keys)
        except redis.RedisError:
            if not self._fallback_allowed():
                raise RuntimeError("Redis unavailable")
            with self._lock:
                for key in [item for item in self._local if item.startswith(redis_prefix)]:
                    self._local.pop(key, None)

    def ping(self) -> bool:
        try:
            return bool(self._redis.ping())
        except redis.RedisError:
            return False


state_store = StateStore()
