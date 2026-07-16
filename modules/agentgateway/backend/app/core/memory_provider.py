"""Memory provider abstraction (Batch D skeleton).

A single seam through which runtime / worker read and write long-term memory.
Business code MUST go through ``get_memory_provider()`` and never call mem0
HTTP/SDK directly (spec: mem0-memory-provider §"Memory provider 抽象与 wiring").

Three implementations selected by ``MEMORY_PROVIDER``:
  - ``none``   : memory disabled — every store is a no-op, retrieve returns [].
  - ``pgonly`` : metadata + body live entirely in PG ``memory_items`` (mem0_ref
                 stays null). The safe default and the fallback target.
  - ``mem0``   : body text + semantic index live in mem0, PG keeps metadata +
                 ``mem0_ref``. Falls back to pgonly when mem0 is unreachable.

This is a skeleton: PG persistence and mem0 wiring are intentionally minimal /
stubbed so later changes (Batch D proper) can fill them in without reshaping
the interface. Nothing here imports a heavy dependency; the mem0 client uses
the stdlib ``urllib`` only, and only when actually configured.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

_log = logging.getLogger(__name__)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# Only semantic recall ever reads mem0. working_state / session transcript /
# agent config are non-semantic and MUST NOT hit mem0 — they live in Redis / PG
# (spec: mem0-memory-provider §"非语义检索不读 mem0").
SEMANTIC_RETRIEVAL_KINDS = frozenset({"semantic", "episodic", "summary", "profile"})


def is_semantic_retrieval_kind(retrieval_kind: Optional[str]) -> bool:
    """Whether a retrieval kind is a semantic recall that may consult mem0."""
    return (retrieval_kind or "").strip().lower() in SEMANTIC_RETRIEVAL_KINDS


@dataclass
class MemoryRecord:
    """Provider-level view of one long-term memory. Mirrors a subset of the PG
    ``memory_items`` columns plus the resolved body text."""
    memory_type: str
    scope: str
    content: str
    summary: str = ""
    importance: float = 0.0
    confidence: float = 0.0
    source_kind: str = "manual"
    source_ref: str = ""
    agent_id: Optional[int] = None
    user_id: Optional[int] = None
    mem0_ref: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class StoreResult:
    """Outcome of a store_* call. ``mem0_ref`` is set only when the body was
    written to mem0; pgonly keeps it null and stores the body in PG."""
    ok: bool
    mem0_ref: Optional[str] = None
    provider: str = "none"
    detail: str = ""


class MemoryProvider:
    """Abstract provider interface. Subclasses MUST implement the four verbs."""

    name = "abstract"

    def health_check(self) -> bool:  # pragma: no cover - trivial default
        return True

    def store_profile_memory(self, record: MemoryRecord) -> StoreResult:
        raise NotImplementedError

    def store_episodic_memory(self, record: MemoryRecord) -> StoreResult:
        raise NotImplementedError

    def store_summary_memory(self, record: MemoryRecord) -> StoreResult:
        raise NotImplementedError

    def retrieve_memories(
        self,
        query: str,
        *,
        scope: Optional[str] = None,
        memory_type: Optional[str] = None,
        agent_id: Optional[int] = None,
        limit: int = 10,
    ) -> List[MemoryRecord]:
        raise NotImplementedError


class NoneMemoryProvider(MemoryProvider):
    """Memory disabled. Stores are no-ops; retrieval is always empty."""

    name = "none"

    def store_profile_memory(self, record: MemoryRecord) -> StoreResult:
        return StoreResult(ok=True, provider=self.name, detail="memory disabled")

    store_episodic_memory = store_profile_memory
    store_summary_memory = store_profile_memory

    def retrieve_memories(self, query: str, **kwargs) -> List[MemoryRecord]:
        return []


class PgOnlyMemoryProvider(MemoryProvider):
    """Body + metadata both in PG ``memory_items``; no external semantic index.

    Skeleton stage: persistence is delegated to ``memory_service`` (which owns
    the Session) rather than embedded here, to keep the provider DB-agnostic.
    The store_* methods return a successful no-mem0 result so callers can record
    the row themselves; retrieval returns [] until the PG query is wired in a
    follow-up change.
    """

    name = "pgonly"

    def _store(self, record: MemoryRecord) -> StoreResult:
        # mem0_ref stays None — body belongs in PG content for this provider.
        return StoreResult(ok=True, mem0_ref=None, provider=self.name)

    def store_profile_memory(self, record: MemoryRecord) -> StoreResult:
        return self._store(record)

    def store_episodic_memory(self, record: MemoryRecord) -> StoreResult:
        return self._store(record)

    def store_summary_memory(self, record: MemoryRecord) -> StoreResult:
        return self._store(record)

    def retrieve_memories(self, query: str, **kwargs) -> List[MemoryRecord]:
        # PG structured retrieval is implemented in session_retrieval_service /
        # memory_service; provider-level semantic retrieval is a no-op here.
        return []


class Mem0MemoryProvider(MemoryProvider):
    """mem0 adapter: env wiring + health check + safe fallback to pgonly.

    Only the wiring and liveness probe are implemented at the skeleton stage.
    Actual store/retrieve calls degrade to the ``pgonly`` behaviour whenever
    mem0 is unconfigured or its health check fails, so a mem0 outage never
    breaks the main chat / writeback path (spec: §"何时读 mem0 与 fallback").
    """

    name = "mem0"

    def __init__(self, base_url: str, api_key: str = "", timeout: float = 3.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self._fallback = PgOnlyMemoryProvider()
        self._last_health = None  # last emitted healthcheck state (None until first probe)

    def health_check(self) -> bool:
        """Probe mem0 liveness. Any failure → False (caller falls back)."""
        if not self.base_url:
            return False
        url = f"{self.base_url}/health"
        req = urllib.request.Request(url, method="GET")
        if self.api_key:
            req.add_header("Authorization", f"Bearer {self.api_key}")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return 200 <= resp.status < 300
        except (urllib.error.URLError, OSError, ValueError) as exc:
            _log.warning("mem0 health check failed (%s); will fall back to pgonly", exc)
            self._emit_healthcheck(False, str(exc))
            return False

    def _emit_healthcheck(self, ok: bool, detail: str = "") -> None:
        """Record a healthcheck memory_event on status change only (avoid
        flooding one row per call). Best-effort — observability never breaks the
        provider."""
        if getattr(self, "_last_health", None) == ok:
            return
        self._last_health = ok
        try:
            from app.core.memory_events import record_memory_event
            record_memory_event(
                provider=self.name, event_type="healthcheck",
                status="success" if ok else "degraded", source="mem0_healthcheck",
                query_or_reason="mem0 liveness probe",
                payload_summary=("mem0 可用" if ok else f"mem0 不可用，降级 pgonly：{detail}"[:2000]),
            )
        except Exception:
            pass

    def _http_json(self, path: str, body: dict) -> Optional[dict]:
        """POST JSON to mem0; return parsed response dict or None on any failure."""
        url = f"{self.base_url}{path}"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        if self.api_key:
            req.add_header("Authorization", f"Bearer {self.api_key}")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            if not (200 <= resp.status < 300):
                raise OSError(f"mem0 returned {resp.status}")
            raw = resp.read().decode("utf-8") or "{}"
            return json.loads(raw)

    def _store(self, record: MemoryRecord) -> StoreResult:
        """Write a memory to mem0; capture the returned id as mem0_ref. Any
        failure (unreachable / non-2xx / parse) degrades to pgonly so the body is
        never lost (spec: 安全降级)."""
        if not self.health_check():
            return self._fallback._store(record)
        try:
            resp = self._http_json("/memories", {
                "content": record.content,
                "metadata": {
                    "memory_type": record.memory_type, "scope": record.scope,
                    "agent_id": record.agent_id, "user_id": record.user_id,
                    "source_kind": record.source_kind, "source_ref": record.source_ref,
                    **(record.metadata or {}),
                },
            })
            # mem0 commonly returns {"id": ...} or {"results":[{"id":...}]}.
            mem0_ref = None
            if isinstance(resp, dict):
                mem0_ref = resp.get("id")
                if mem0_ref is None and isinstance(resp.get("results"), list) and resp["results"]:
                    first = resp["results"][0]
                    mem0_ref = first.get("id") if isinstance(first, dict) else None
            if mem0_ref is None:
                raise ValueError("mem0 store response missing id")
            return StoreResult(ok=True, mem0_ref=str(mem0_ref), provider=self.name)
        except Exception as exc:
            _log.warning("mem0 store failed (%s); degrading to pgonly", exc)
            return self._fallback._store(record)

    def store_profile_memory(self, record: MemoryRecord) -> StoreResult:
        return self._store(record)

    def store_episodic_memory(self, record: MemoryRecord) -> StoreResult:
        return self._store(record)

    def store_summary_memory(self, record: MemoryRecord) -> StoreResult:
        return self._store(record)

    def retrieve_memories(self, query: str, **kwargs) -> List[MemoryRecord]:
        """Semantic recall from mem0. Any failure degrades to pgonly's behaviour
        (returns []) so the caller is never broken."""
        if not self.health_check():
            return self._fallback.retrieve_memories(query, **kwargs)
        try:
            resp = self._http_json("/search", {
                "query": query,
                "scope": kwargs.get("scope"),
                "memory_type": kwargs.get("memory_type"),
                "agent_id": kwargs.get("agent_id"),
                "limit": kwargs.get("limit", 10),
            })
            results = []
            rows = resp.get("results") if isinstance(resp, dict) else None
            for r in rows or []:
                if not isinstance(r, dict):
                    continue
                meta = r.get("metadata") or {}
                results.append(MemoryRecord(
                    memory_type=meta.get("memory_type", kwargs.get("memory_type") or "semantic"),
                    scope=meta.get("scope", kwargs.get("scope") or ""),
                    content=r.get("content") or r.get("memory") or "",
                    summary=r.get("summary", ""),
                    importance=float(r.get("score", 0.0) or 0.0),
                    source_kind=meta.get("source_kind", "manual"),
                    source_ref=meta.get("source_ref", ""),
                    agent_id=meta.get("agent_id"),
                    user_id=meta.get("user_id"),
                    mem0_ref=str(r.get("id")) if r.get("id") is not None else None,
                    metadata=meta,
                ))
            return results
        except Exception as exc:
            _log.warning("mem0 retrieve failed (%s); degrading to pgonly", exc)
            return self._fallback.retrieve_memories(query, **kwargs)


def _select_provider_name() -> str:
    """Resolve the effective provider name from env, honoring ENABLE_MEM0."""
    name = (os.environ.get("MEMORY_PROVIDER") or "none").strip().lower()
    if name not in {"none", "pgonly", "mem0"}:
        _log.warning("unknown MEMORY_PROVIDER=%r; defaulting to none", name)
        return "none"
    if name == "mem0" and not _env_bool("ENABLE_MEM0", False):
        # mem0 selected but the kill-switch is off → pgonly (still persists).
        return "pgonly"
    return name


def build_memory_provider() -> MemoryProvider:
    """Construct the provider for the current environment, with safe fallback.

    mem0 selection that can't be wired (missing base URL or failing health
    check) degrades to pgonly rather than raising, so callers always get a
    working provider.
    """
    name = _select_provider_name()
    if name == "none":
        return NoneMemoryProvider()
    if name == "pgonly":
        return PgOnlyMemoryProvider()
    # name == "mem0"
    base_url = (os.environ.get("MEM0_BASE_URL") or "").strip()
    api_key = (os.environ.get("MEM0_API_KEY") or "").strip()
    if not base_url:
        _log.warning("MEMORY_PROVIDER=mem0 but MEM0_BASE_URL unset; falling back to pgonly")
        return PgOnlyMemoryProvider()
    provider = Mem0MemoryProvider(base_url=base_url, api_key=api_key)
    if not provider.health_check():
        _log.warning("mem0 health check failed at startup; falling back to pgonly")
        return PgOnlyMemoryProvider()
    return provider


_provider_singleton: Optional[MemoryProvider] = None


def should_read_mem0(
    retrieval_kind: Optional[str],
    provider: Optional[MemoryProvider] = None,
) -> bool:
    """Decide whether a given retrieval should consult mem0.

    Reads mem0 only when ALL hold (spec §"何时读 mem0 与 fallback"):
      - the retrieval is a semantic recall (semantic/episodic/summary/profile),
      - ``ENABLE_MEM0=true``,
      - the effective provider is the live mem0 adapter (its health passes).

    Non-semantic retrieval (working state / session transcript / agent config),
    a disabled kill-switch, or a provider that already degraded to pgonly all
    return False so the caller stays on Redis / PG and never blocks on mem0.
    """
    if not is_semantic_retrieval_kind(retrieval_kind):
        return False
    if not _env_bool("ENABLE_MEM0", False):
        return False
    prov = provider if provider is not None else get_memory_provider()
    if not isinstance(prov, Mem0MemoryProvider):
        # build_memory_provider already degraded to pgonly/none → don't read mem0.
        return False
    return prov.health_check()


def get_memory_provider(refresh: bool = False) -> MemoryProvider:
    """Return the process-wide provider singleton (lazy, env-driven)."""
    global _provider_singleton
    if _provider_singleton is None or refresh:
        _provider_singleton = build_memory_provider()
    return _provider_singleton
