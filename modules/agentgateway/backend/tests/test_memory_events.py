"""Backend tests for change: mem0-observability-and-verification (T9).

Covers spec mem0-observability:
- mem0 store captures mem0_ref; failure degrades to pgonly (no exception)
- record_memory_item / retrieve emit write / recall memory_events
- healthcheck failure emits a degraded healthcheck event
- review/audit read-only API shape
- write → recall → memory_used_explanation (mem0 mocked, no network)

All mem0 HTTP is mocked — no network. Uses the disk/none-provider test harness
from conftest (ENABLE_MINIO/JOB_QUEUE pinned there).
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.api import planner as planner_api
from app.core import memory_events
from app.core.memory_provider import Mem0MemoryProvider, MemoryRecord, StoreResult
from app.models.db import MemoryEvent, MemoryItem


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(planner_api.router, prefix="/api")
    return TestClient(app)


# ─── 7.1 mem0 store captures ref / degrades ─────────────────────────────────

def test_mem0_store_captures_ref(monkeypatch):
    p = Mem0MemoryProvider(base_url="http://mem0.test", api_key="k")
    monkeypatch.setattr(p, "health_check", lambda: True)
    monkeypatch.setattr(p, "_http_json", lambda path, body: {"id": "mem0-123"})
    rec = MemoryRecord(memory_type="profile_preference", scope="user", content="先给结论再给细节")
    res = p.store_profile_memory(rec)
    assert res.ok and res.mem0_ref == "mem0-123" and res.provider == "mem0"


def test_mem0_store_degrades_on_failure(monkeypatch):
    p = Mem0MemoryProvider(base_url="http://mem0.test")
    monkeypatch.setattr(p, "health_check", lambda: True)
    def _boom(path, body):
        raise OSError("mem0 down")
    monkeypatch.setattr(p, "_http_json", _boom)
    rec = MemoryRecord(memory_type="episodic", scope="agent", content="x")
    res = p.store_episodic_memory(rec)
    # degraded to pgonly: ok, no mem0_ref, no exception
    assert res.ok and res.mem0_ref is None and res.provider == "pgonly"


def test_mem0_retrieve_parses_results(monkeypatch):
    p = Mem0MemoryProvider(base_url="http://mem0.test")
    monkeypatch.setattr(p, "health_check", lambda: True)
    monkeypatch.setattr(p, "_http_json", lambda path, body: {"results": [
        {"id": "m1", "content": "客户偏好 A", "metadata": {"scope": "user", "memory_type": "semantic"}},
    ]})
    recs = p.retrieve_memories("客户偏好", scope="user", limit=5)
    assert len(recs) == 1 and recs[0].content == "客户偏好 A" and recs[0].mem0_ref == "m1"


# ─── 7.2 write / recall emit events ─────────────────────────────────────────

def test_record_memory_event_persists():
    memory_events.record_memory_event(
        provider="mem0", event_type="write", status="success",
        run_id="r-w1", scope="user", source="proposal_confirmation",
        query_or_reason="write profile_preference", payload_summary="先给结论",
        details={"memory_type": "profile_preference", "mem0_ref": "x1"},
    )
    rows = memory_events.list_memory_events("r-w1")
    assert rows and rows[0]["event_type"] == "write" and rows[0]["details"]["mem0_ref"] == "x1"


def test_retrieve_emits_recall_event(monkeypatch):
    from app.core import memory_service
    # Force a known provider + result so recall emits a recall event.
    import app.core.memory_provider as mp

    class _FakeProv:
        name = "mem0"
        def retrieve_memories(self, query, **kw):
            return [MemoryRecord(memory_type="semantic", scope="user", content="hit")]
    monkeypatch.setattr(memory_service, "get_memory_provider", lambda: _FakeProv())
    before = len(memory_events.list_memory_events("")) if False else None
    out = memory_service.retrieve_semantic_memories("查询X", scope="user")
    assert len(out) == 1
    # a recall event was recorded (source=semantic_recall)
    from app.core.database import engine
    with Session(engine) as s:
        ev = s.exec(select(MemoryEvent).where(
            MemoryEvent.event_type == "recall", MemoryEvent.source == "semantic_recall"
        ).order_by(MemoryEvent.id.desc())).first()
    assert ev is not None and "1" in ev.payload_summary


# ─── 7.2b healthcheck degraded event ────────────────────────────────────────

def test_healthcheck_failure_emits_event(monkeypatch):
    p = Mem0MemoryProvider(base_url="http://127.0.0.1:1")  # nothing listens
    # urlopen will fail → health_check False → _emit_healthcheck(False)
    ok = p.health_check()
    assert ok is False
    from app.core.database import engine
    with Session(engine) as s:
        ev = s.exec(select(MemoryEvent).where(
            MemoryEvent.event_type == "healthcheck", MemoryEvent.status == "degraded"
        ).order_by(MemoryEvent.id.desc())).first()
    assert ev is not None and ev.provider == "mem0"


# ─── 7.3 emit_memory_event push + persist ───────────────────────────────────

class _FakeWS:
    def __init__(self):
        self.sent = []
    async def send_json(self, payload):
        self.sent.append(payload)


@pytest.mark.asyncio
async def test_emit_memory_event_persists_and_pushes():
    ws = _FakeWS()
    await memory_events.emit_memory_event(
        websocket=ws, provider="mem0", event_type="recall", status="success",
        run_id="r-emit", source="planner_recall", query_or_reason="goal",
        payload_summary="命中记忆", details={"memory_used_explanation": ["画像：X"]},
    )
    # persisted
    rows = memory_events.list_memory_events("r-emit")
    assert rows and rows[0]["source"] == "planner_recall"
    # pushed with same shape
    assert ws.sent and ws.sent[0]["type"] == "memory_event"
    assert ws.sent[0]["details"]["memory_used_explanation"] == ["画像：X"]


@pytest.mark.asyncio
async def test_emit_memory_event_no_socket_only_persists():
    await memory_events.emit_memory_event(
        websocket=None, provider="pgonly", event_type="recall", status="success",
        run_id="r-nosock", source="planner_recall",
    )
    rows = memory_events.list_memory_events("r-nosock")
    assert rows and rows[0]["provider"] == "pgonly"  # persisted, no push, no error


# ─── 7.5 review/audit API ───────────────────────────────────────────────────

def test_memory_events_api(client: TestClient):
    memory_events.record_memory_event(
        provider="mem0", event_type="write", status="success", run_id="r-api1",
        source="x", payload_summary="s",
    )
    resp = client.get("/api/planner/memory-events/by-run/r-api1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["run_id"] == "r-api1" and any(e["event_type"] == "write" for e in body["events"])


def test_memory_items_audit_api(client: TestClient):
    with Session(planner_api.engine) as s:
        s.add(MemoryItem(memory_type="profile_preference", scope="user",
                         content="先结论后细节", summary="先结论后细节",
                         source_kind="proposal", status="active", access_count=2))
        s.commit()
    resp = client.get("/api/planner/memory-items/audit", params={"scope": "user"})
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert any(it["used"] is True and it["scope"] == "user" for it in items)


# ─── 7.6 provider gating unchanged ──────────────────────────────────────────

def test_provider_gating_unchanged(monkeypatch):
    import app.core.memory_provider as mp
    for k in ("MEMORY_PROVIDER", "ENABLE_MEM0", "MEM0_BASE_URL", "MEM0_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    # default → none; mem0 without enable flag → pgonly (existing contract)
    assert mp.build_memory_provider().name == "none"
    monkeypatch.setenv("MEMORY_PROVIDER", "mem0")
    monkeypatch.setenv("MEM0_BASE_URL", "http://127.0.0.1:0")
    assert mp.build_memory_provider().name == "pgonly"
