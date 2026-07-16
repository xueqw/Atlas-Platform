"""Tests for implement-redis-state-and-job-queue-batch-c (redis-state-and-job-queue).

Covers the change's acceptance scenarios:
- 5.1 redis_client 回落：禁用→进程内；配置但 ping 失败→降级（不连真实 Redis）
- 5.2 锁：acquire/release、token 校验释放、TTL 到期可重获取
- 5.3 working state：进程内存取语义；fakeredis round-trip
- 5.4 apply-replan 幂等：同 key 并发只放行一个（锁语义）
- 5.5 job_queue：两 provider enqueue/dequeue、幂等去重、状态机、inprocess 不 push

Redis is exercised via fakeredis (no real server). The in-process path is forced
through the `_set_backend_for_test` seam so tests are deterministic regardless of
the developer's `.env` ENABLE_REDIS value.
"""

from __future__ import annotations

import time

import fakeredis
import pytest
from sqlmodel import Session, select

from app.core import redis_client as rc
from app.core.database import engine
from app.models.db import MemoryWritebackJob


@pytest.fixture
def inproc_backend():
    """Force the in-process backend for the duration of a test, then restore."""
    prev = rc._backend
    rc._set_backend_for_test(rc._InProcessBackend())
    yield rc._backend
    rc._set_backend_for_test(prev)


@pytest.fixture
def fakeredis_backend():
    """Force a fakeredis-backed Redis backend (exercises real Redis semantics)."""
    prev = rc._backend
    client = fakeredis.FakeStrictRedis()
    rc._set_backend_for_test(rc._RedisBackend(client))
    yield rc._backend
    rc._set_backend_for_test(prev)


# ─── 5.1 回落 ────────────────────────────────────────────────────────────────

class TestBackendSelection:
    def test_disabled_uses_inprocess(self, monkeypatch):
        monkeypatch.setenv("ENABLE_REDIS", "false")
        assert rc._build_backend().name == "inprocess"

    def test_enabled_no_url_falls_back(self, monkeypatch):
        monkeypatch.setenv("ENABLE_REDIS", "true")
        monkeypatch.delenv("REDIS_URL", raising=False)
        assert rc._build_backend().name == "inprocess"

    def test_enabled_unreachable_falls_back(self, monkeypatch):
        monkeypatch.setenv("ENABLE_REDIS", "true")
        # A port nothing listens on → ping fails → in-process fallback, no raise.
        monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
        assert rc._build_backend().name == "inprocess"


# ─── 5.2 锁 ──────────────────────────────────────────────────────────────────

class TestLocks:
    def _run(self, backend):
        t = rc.acquire_lock("L", ttl=30)
        assert t
        assert rc.acquire_lock("L", ttl=30) is None  # held
        assert rc.release_lock("L", "wrong-token") is False  # not owner
        assert rc.release_lock("L", t) is True  # owner releases
        assert rc.acquire_lock("L", ttl=30)  # reacquire after release

    def test_locks_inprocess(self, inproc_backend):
        self._run(inproc_backend)

    def test_locks_redis(self, fakeredis_backend):
        self._run(fakeredis_backend)

    def test_ttl_expiry_inprocess(self, inproc_backend):
        t = rc.acquire_lock("T", ttl=1)
        assert t
        assert rc.acquire_lock("T", ttl=1) is None
        time.sleep(1.1)
        assert rc.acquire_lock("T", ttl=1)  # expired → reacquirable


# ─── 5.3 cache / working state round-trip ────────────────────────────────────

class TestCache:
    def test_inprocess_roundtrip(self, inproc_backend):
        rc.set_cache("k", {"a": 1})
        assert rc.get_cache("k") == {"a": 1}  # object preserved by reference
        rc.delete_cache("k")
        assert rc.get_cache("k") is None

    def test_redis_roundtrip_string(self, fakeredis_backend):
        rc.set_cache("k", "hello")
        assert rc.get_cache("k") == "hello"
        rc.delete_cache("k")
        assert rc.get_cache("k") is None


class TestWorkingState:
    def test_store_load_drop_inprocess(self, inproc_backend):
        from app.api.planner import state
        cd = {"messages": [], "memory": {}, "mode": "create"}
        state.store_conv("conv-1", cd)
        assert state.has_conv("conv-1")
        assert state.load_conv("conv-1") == cd
        state.drop_conv("conv-1")
        assert state.load_conv("conv-1") is None

    def test_store_load_redis_json(self, fakeredis_backend):
        from app.api.planner import state
        cd = {"messages": [{"role": "user", "content": "hi"}], "mode": "replan"}
        state.store_conv("conv-2", cd)
        loaded = state.load_conv("conv-2")
        assert loaded == cd  # JSON round-trip equal


# ─── 5.4 apply-replan 幂等锁 ─────────────────────────────────────────────────

class TestReplanIdempotencyLock:
    def test_concurrent_same_key_one_winner(self, inproc_backend):
        name = "replan:7:conv-9"
        first = rc.acquire_lock(name, ttl=60)
        assert first
        # A concurrent apply for the same (agent, conversation) cannot acquire.
        assert rc.acquire_lock(name, ttl=60) is None
        rc.release_lock(name, first)
        # After the first finishes, the key frees up.
        assert rc.acquire_lock(name, ttl=60)


# ─── 5.5 job_queue ───────────────────────────────────────────────────────────

class TestJobQueue:
    def test_inprocess_enqueue_no_push(self, inproc_backend, monkeypatch):
        monkeypatch.setenv("JOB_QUEUE_PROVIDER", "inprocess")
        from app.core import job_queue as jq
        job = jq.enqueue(job_type="extract_profile", source_kind="conversation", source_ref="c-ip-1")
        assert job is not None and job.status == "pending"
        # inprocess: nothing pushed, dequeue is None
        assert jq.dequeue() is None

    def test_idempotent_dedupe(self, inproc_backend, monkeypatch):
        monkeypatch.setenv("JOB_QUEUE_PROVIDER", "inprocess")
        from app.core import job_queue as jq
        j1 = jq.enqueue(job_type="extract_episodic", source_kind="proposal", source_ref="c-dedupe")
        j2 = jq.enqueue(job_type="extract_episodic", source_kind="proposal", source_ref="c-dedupe")
        assert j1.id == j2.id  # pending exists → no duplicate

    def test_invalid_job_type(self, inproc_backend):
        from app.core import job_queue as jq
        assert jq.enqueue(job_type="bogus", source_kind="x", source_ref="y") is None

    def test_state_machine_and_failed_reenqueue(self, inproc_backend, monkeypatch):
        monkeypatch.setenv("JOB_QUEUE_PROVIDER", "inprocess")
        from app.core import job_queue as jq
        job = jq.enqueue(job_type="merge_semantic", source_kind="run_summary", source_ref="c-sm")
        jq.mark_running(job.id)
        with Session(engine) as s:
            assert s.get(MemoryWritebackJob, job.id).status == "running"
        jq.mark_failed(job.id, "boom")
        with Session(engine) as s:
            row = s.get(MemoryWritebackJob, job.id)
            assert row.status == "failed" and row.error == "boom" and row.finished_at is not None
        # failed is terminal for dedupe → a new enqueue creates a fresh job
        job2 = jq.enqueue(job_type="merge_semantic", source_kind="run_summary", source_ref="c-sm")
        assert job2.id != job.id

    def test_redis_enqueue_pushes_envelope(self, fakeredis_backend, monkeypatch):
        monkeypatch.setenv("JOB_QUEUE_PROVIDER", "redis")
        from app.core import job_queue as jq
        job = jq.enqueue(job_type="compress_summary", source_kind="evaluation", source_ref="c-rd")
        assert job is not None
        env = jq.dequeue()
        assert env is not None and env["id"] == job.id and env["job_type"] == "compress_summary"
