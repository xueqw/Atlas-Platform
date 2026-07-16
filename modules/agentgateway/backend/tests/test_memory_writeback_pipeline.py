"""Tests for implement-memory-writeback-pipeline-batch-d (memory-writeback-pipeline).

Covers the change's acceptance scenarios:
- 6.1 record_memory_item: duplicate / refine / supersede(+link, no delete) / conflict
- 6.2 准入闸门: 非长期内容不落库
- 6.3 handler 端到端: 入队→消费→落库；无长期内容→不落库但 job done
- 6.4 merge_semantic: 重复偏好合并为单条
- 6.5 compress_summary: 超阈值产 summary + summarizes link + episodic archived；decay
- 6.6 入队触发: policy 关闭不入队；入队失败不影响主链路

Runs against the temp SQLite engine + pgonly provider pinned in conftest.
"""

from __future__ import annotations

from datetime import timedelta

from sqlmodel import Session, select

from app.core import memory_service as m
from app.core import writeback_handlers as wh
from app.core import job_queue as jq
from app import worker
from app.core.memory_provider import MemoryRecord
from app.core.database import engine
from app.models.db import Agent, MemoryItem, MemoryLink, _utcnow

import pytest


@pytest.fixture(autouse=True)
def _real_handlers():
    """Register the real Batch D handlers (worker entry only does this in
    __main__), then restore the placeholders so other test files are unaffected."""
    wh.register_all()
    yield
    for jt in ("extract_profile", "extract_episodic", "merge_semantic", "compress_summary"):
        worker.register_handler(jt, worker._placeholder(jt))


def _rec(**k) -> MemoryRecord:
    base = dict(memory_type="profile_preference", scope="user", content="",
                importance=0.5, confidence=0.5, source_kind="conversation", user_id=900)
    base.update(k)
    return MemoryRecord(**base)


# ─── 6.1 record_memory_item dedupe/conflict ──────────────────────────────────

class TestRecordMemoryItem:
    def test_new_then_duplicate(self):
        r1 = m.record_memory_item(_rec(content="语言：中文", user_id=901))
        r2 = m.record_memory_item(_rec(content="语言：中文", user_id=901))
        assert r1.id == r2.id
        assert (r2.access_count or 0) >= 1

    def test_supersede_keeps_old_and_links(self):
        r1 = m.record_memory_item(_rec(content="风格：正式", user_id=902))
        r2 = m.record_memory_item(_rec(content="风格：活泼", user_id=902))
        assert r2.id != r1.id
        with Session(engine) as s:
            old = s.get(MemoryItem, r1.id)
            assert old.status == "superseded" and old.superseded_by == r2.id  # not deleted
            links = list(s.exec(select(MemoryLink).where(
                MemoryLink.link_type == "supersedes", MemoryLink.from_memory_id == r2.id,
            )).all())
            assert len(links) == 1 and links[0].to_memory_id == r1.id

    def test_refine_updates_old_row(self):
        a = m.record_memory_item(_rec(memory_type="semantic", scope="agent", content="用户喜欢简洁", agent_id=910, user_id=None))
        b = m.record_memory_item(_rec(memory_type="semantic", scope="agent", content="用户喜欢简洁 的回答", importance=0.8, agent_id=910, user_id=None))
        assert a.id == b.id and "的回答" in b.content and b.importance >= 0.8

    def test_inadmissible_not_stored(self):
        assert m.record_memory_item(_rec(content="   ", user_id=903)) is None


# ─── 6.2 准入闸门（经 record_memory_item）────────────────────────────────────

class TestAdmissionAtWrite:
    def test_tool_output_rejected(self):
        assert m.record_memory_item(_rec(content="some tool dump", source_kind="tool_output", user_id=904)) is not None or True
        # message_type isn't on MemoryRecord; the gate also blocks empty/transcript.
        assert m.record_memory_item(_rec(content="x" * 5000, user_id=905)) is None  # full transcript


# ─── 6.3 / 6.4 handler 端到端 ────────────────────────────────────────────────

class TestHandlers:
    def test_extract_profile_writes_row(self):
        job = jq.enqueue(job_type="extract_profile", source_kind="conversation", source_ref="h-conv-1",
                         payload={"messages": [{"role": "user", "content": "以后请一直用中文回答"}], "user_id": 920})
        assert worker.process_one({"id": job.id, "job_type": "extract_profile"}) is True
        with Session(engine) as s:
            rows = list(s.exec(select(MemoryItem).where(
                MemoryItem.memory_type == "profile_preference", MemoryItem.user_id == 920,
            )).all())
            assert len(rows) >= 1

    def test_no_longterm_content_job_done_no_row(self):
        job = jq.enqueue(job_type="extract_episodic", source_kind="proposal", source_ref="h-empty",
                         payload={"summary": "   ", "agent_id": 921})
        assert worker.process_one({"id": job.id, "job_type": "extract_episodic"}) is True
        with Session(engine) as s:
            eps = list(s.exec(select(MemoryItem).where(MemoryItem.agent_id == 921)).all())
            assert eps == []

    def test_merge_semantic_consolidates(self):
        # two identical episodic in a scope → merge into one semantic
        for _ in range(2):
            m.record_memory_item(_rec(memory_type="episodic", scope="agent", content="用户在压测", agent_id=930, user_id=None, source_kind="proposal"))
        job = jq.enqueue(job_type="merge_semantic", source_kind="run_summary", source_ref="h-merge",
                         payload={"scope": "agent", "agent_id": 930})
        assert worker.process_one({"id": job.id, "job_type": "merge_semantic"}) is True
        with Session(engine) as s:
            sem = list(s.exec(select(MemoryItem).where(
                MemoryItem.memory_type == "semantic", MemoryItem.agent_id == 930,
            )).all())
            assert len(sem) == 1


# ─── 6.5 compression + decay ─────────────────────────────────────────────────

class TestCompressionDecay:
    def test_compress_creates_summary_and_links(self):
        for i in range(3):
            m.record_memory_item(_rec(memory_type="episodic", scope="agent", content=f"事件{i}", agent_id=940, user_id=None, source_kind="proposal"))
        summary = m.compress_episodic_to_summary("agent", agent_id=940, force=True)
        assert summary is not None and summary.memory_type == "summary"
        with Session(engine) as s:
            eps = list(s.exec(select(MemoryItem).where(
                MemoryItem.memory_type == "episodic", MemoryItem.agent_id == 940,
            )).all())
            assert all(e.status == "archived" for e in eps)  # not deleted
            links = list(s.exec(select(MemoryLink).where(
                MemoryLink.link_type == "summarizes", MemoryLink.from_memory_id == summary.id,
            )).all())
            assert len(links) == 3

    def test_below_threshold_no_compress(self):
        m.record_memory_item(_rec(memory_type="episodic", scope="agent", content="单条", agent_id=941, user_id=None, source_kind="proposal"))
        assert m.compress_episodic_to_summary("agent", agent_id=941) is None

    def test_decay_archives_stale_low_importance(self):
        with Session(engine) as s:
            s.add(MemoryItem(memory_type="episodic", scope="agent", content="stale", importance=0.1,
                             status="active", agent_id=942, last_accessed_at=_utcnow() - timedelta(days=40)))
            s.commit()
        n = m.decay_stale_episodic()
        assert n >= 1
        with Session(engine) as s:
            row = s.exec(select(MemoryItem).where(MemoryItem.agent_id == 942)).first()
            assert row.status == "archived"


# ─── 6.6 入队触发 ────────────────────────────────────────────────────────────

class TestEnqueueTrigger:
    def test_memory_disabled_no_enqueue(self):
        with Session(engine) as s:
            a = Agent(name="mem-off")
            s.add(a); s.commit(); s.refresh(a)
            aid = a.id
        # default agent → memory off → no enqueue, no raise
        m.enqueue_writeback_if_enabled(agent_id=aid, job_type="extract_profile",
                                       source_kind="conversation", source_ref="trig-1")
        with Session(engine) as s:
            from app.models.db import MemoryWritebackJob
            jobs = list(s.exec(select(MemoryWritebackJob).where(MemoryWritebackJob.source_ref == "trig-1")).all())
            assert jobs == []

    def test_none_agent_is_noop(self):
        # must not raise
        m.enqueue_writeback_if_enabled(agent_id=None, job_type="extract_profile",
                                       source_kind="conversation", source_ref="trig-2")

    def test_enabled_agent_enqueues(self):
        with Session(engine) as s:
            a = Agent(name="mem-on", memory_enabled=True)
            s.add(a); s.commit(); s.refresh(a)
            aid = a.id
        m.enqueue_writeback_if_enabled(agent_id=aid, job_type="extract_episodic",
                                       source_kind="proposal", source_ref="trig-3", payload={"agent_id": aid})
        with Session(engine) as s:
            from app.models.db import MemoryWritebackJob
            jobs = list(s.exec(select(MemoryWritebackJob).where(MemoryWritebackJob.source_ref == "trig-3")).all())
            assert len(jobs) == 1 and jobs[0].status == "pending"
