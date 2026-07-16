"""Backend tests for change: planner-run-events (T8).

Drives the real ws turn loop with FakeWebSocket + scripted run_conversation and
asserts the persisted+streamed run-event timeline. Covers spec planner-run-events:
- normalized step/status set; persisted, queryable by run_id
- run_event stream coexists with the unchanged activity stream
- proposal_compose event carries proposal_id
- failed step recorded; turn not aborted
- stuck-step message is specific; read-only API
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.api import planner as planner_api
from app.api.planner import ws as planner_ws
from app.api.planner import state as planner_state
from app.core import run_events
from app.models.db import PlannerRunEvent


class FakeWebSocket:
    def __init__(self, inbound):
        self._inbound = list(inbound)
        self.sent = []

    async def accept(self):
        return None

    async def receive_text(self):
        if not self._inbound:
            from fastapi import WebSocketDisconnect
            raise WebSocketDisconnect()
        return json.dumps(self._inbound.pop(0))

    async def send_json(self, payload):
        self.sent.append(payload)


def _script_stream(events):
    async def _gen(agent, user_message, attachments=None):
        for ev in events:
            yield ev
    return _gen


def _of_type(sent, t):
    return [m for m in sent if m.get("type") == t]


def _proposal_block():
    proposal = {
        "ready": True,
        "proposal": {
            "title": "销售日报 Agent", "goal": "g", "runtime_mode": "cron",
            "deliverables": ["日报"], "recommended_capabilities": [],
            "context_used_explanation": ["基于销售总监角色"],
            "nodes": [{"id": "agent1", "type": "agent",
                       "config": {"system_prompt": "x", "model_name": "qwen3.6-27b", "provider": "glm"}}],
            "edges": [],
        },
    }
    return "```json\n" + json.dumps(proposal, ensure_ascii=False) + "\n```"


@pytest.fixture
def patched(monkeypatch):
    monkeypatch.setattr(planner_ws, "create_agent", lambda **kw: object())
    monkeypatch.setattr(planner_ws, "_get_langfuse", lambda: None)
    monkeypatch.setattr(planner_ws, "_persist_session", lambda *a, **k: None)
    return monkeypatch


def _seed_conv(cid):
    planner_state.store_conv(cid, {
        "messages": [], "model": {"model_name": "qwen3.6-27b", "provider": "glm"},
        "memory": planner_state._new_memory(), "file_artifacts": [],
    })


# ─── 6.1 step/status constants ──────────────────────────────────────────────

def test_step_and_status_constants():
    assert set(run_events.RUN_STEPS) == {
        "intent_inference", "context_load", "memory_recall",
        "expert_retrieval", "capability_match", "proposal_compose",
    }
    assert set(run_events.RUN_STATUSES) == {"queued", "running", "completed", "failed"}


# ─── 6.2 persisted timeline by run_id ───────────────────────────────────────

@pytest.mark.asyncio
async def test_run_events_persisted_for_proposal_turn(patched):
    patched.setattr(planner_ws, "run_conversation", _script_stream([
        ("token", "方案如下。"),
        ("token", _proposal_block()),
        ("done", ""),
    ]))
    _seed_conv("conv-re1")
    sock = FakeWebSocket([{"type": "message", "content": "做个 sales 销售日报 agent"}])
    await planner_ws.planner_websocket(sock, "conv-re1")

    run_events_pushed = _of_type(sock.sent, "run_event")
    assert run_events_pushed, "run_event must be streamed"
    run_id = run_events_pushed[0]["run_id"]
    with Session(planner_api.engine) as s:
        rows = s.exec(select(PlannerRunEvent).where(PlannerRunEvent.run_id == run_id)
                      .order_by(PlannerRunEvent.id)).all()
    steps = {r.step for r in rows}
    # the proposal turn exercises all six normalized stages
    assert {"intent_inference", "memory_recall", "context_load",
            "expert_retrieval", "capability_match", "proposal_compose"} <= steps


# ─── 6.3 coexistence with activity ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_event_coexists_with_activity(patched):
    patched.setattr(planner_ws, "run_conversation", _script_stream([
        ("token", "你好。" * 3),
        ("done", ""),
    ]))
    _seed_conv("conv-re2")
    sock = FakeWebSocket([{"type": "message", "content": "随便聊聊"}])
    await planner_ws.planner_websocket(sock, "conv-re2")
    # both streams present; activity unchanged (still has phase/label)
    assert _of_type(sock.sent, "activity"), "activity stream must remain"
    assert _of_type(sock.sent, "run_event"), "run_event stream must be added"
    act = _of_type(sock.sent, "activity")[0]
    assert "phase" in act and "label" in act  # activity shape unchanged


# ─── 6.4 proposal_id linkage ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_proposal_compose_event_has_proposal_id(patched):
    patched.setattr(planner_ws, "run_conversation", _script_stream([
        ("token", "方案。"),
        ("token", _proposal_block()),
        ("done", ""),
    ]))
    _seed_conv("conv-re3")
    sock = FakeWebSocket([{"type": "message", "content": "做个 agent"}])
    await planner_ws.planner_websocket(sock, "conv-re3")
    compose = [e for e in _of_type(sock.sent, "run_event")
               if e["step"] == "proposal_compose" and e["status"] == "completed"]
    assert compose and compose[0]["proposal_id"] is not None
    pid = compose[0]["proposal_id"]
    by_prop = run_events.list_run_events_by_proposal(pid)
    assert any(e["step"] == "proposal_compose" for e in by_prop)


# ─── 6.5 failed step does not abort turn ────────────────────────────────────

@pytest.mark.asyncio
async def test_failed_stage_recorded_turn_continues(patched, monkeypatch):
    # Break planning_context aggregation → context_load failed event, turn continues.
    def _boom(**kw):
        raise RuntimeError("ctx boom")
    monkeypatch.setattr("app.core.planning_context.build_planning_context", _boom)
    patched.setattr(planner_ws, "run_conversation", _script_stream([
        ("token", "你好。"),
        ("done", ""),
    ]))
    _seed_conv("conv-re4")
    sock = FakeWebSocket([{"type": "message", "content": "hi"}])
    await planner_ws.planner_websocket(sock, "conv-re4")
    assert _of_type(sock.sent, "done"), "turn completes despite a failed stage"
    failed = [e for e in _of_type(sock.sent, "run_event")
              if e["step"] == "context_load" and e["status"] == "failed"]
    assert failed, "context_load failure recorded as failed run_event"


# ─── 6.6 stuck-step specificity + read-only API ─────────────────────────────

@pytest.mark.asyncio
async def test_capability_match_message_is_specific(patched):
    patched.setattr(planner_ws, "run_conversation", _script_stream([
        ("token", "方案。"),
        ("token", _proposal_block()),
        ("done", ""),
    ]))
    _seed_conv("conv-re5")
    sock = FakeWebSocket([{"type": "message", "content": "做个 agent"}])
    await planner_ws.planner_websocket(sock, "conv-re5")
    cap = [e for e in _of_type(sock.sent, "run_event")
           if e["step"] == "capability_match" and e["status"] == "completed"]
    assert cap and ("候选能力" in cap[0]["message"] or "candidate_count" in cap[0]["details"])


def test_read_only_api(patched):
    # seed events directly via the emitter, then read back
    import asyncio
    emitter = run_events.RunEventEmitter(run_id="run-api-test")
    asyncio.run(emitter.emit("context_load", "completed", message="done"))
    app = FastAPI()
    app.include_router(planner_api.router, prefix="/api")
    client = TestClient(app)
    resp = client.get("/api/planner/runs/run-api-test/events")
    assert resp.status_code == 200
    body = resp.json()
    assert body["run_id"] == "run-api-test"
    assert any(e["step"] == "context_load" for e in body["events"])
