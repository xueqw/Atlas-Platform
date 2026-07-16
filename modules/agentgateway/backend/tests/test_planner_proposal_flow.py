"""Backend tests for change: planner-proposal-flow (T2).

Drives the real planner WebSocket turn loop with a fake socket + scripted
``run_conversation`` (same harness as test_planner_skill_transparency), plus
unit tests on the schema / validation / heuristic helpers. Covers spec
planner-proposal-flow:
- planner consumes planning_context (structured block injected, no raw content)
- structured intermediate output / Proposal Schema validation
- plan-first: no-clarification vs minimal-clarification
- validation-failure degrades without aborting the turn
- feature flag falls back to the legacy freeform path
"""

from __future__ import annotations

import json

import pytest

from app.api.planner import ws as planner_ws
from app.api.planner import state as planner_state
from app.api.planner.proposal import (
    _validate_proposal_payload,
    _match_capability_candidates,
)
from app.api.planner.prompts import _render_context_block
from app.api.planner.schemas import PlannerIntermediate, ProposalPayload


# ─── fixtures / harness ─────────────────────────────────────────────────────

class FakeWebSocket:
    def __init__(self, inbound: list[dict]):
        self._inbound = list(inbound)
        self.sent: list[dict] = []

    async def accept(self):
        return None

    async def receive_text(self) -> str:
        if not self._inbound:
            from fastapi import WebSocketDisconnect
            raise WebSocketDisconnect()
        return json.dumps(self._inbound.pop(0))

    async def send_json(self, payload: dict):
        self.sent.append(payload)


def _script_stream(events):
    async def _gen(agent, user_message, attachments=None):
        for ev in events:
            yield ev
    return _gen


def _of_type(sent, t):
    return [m for m in sent if m.get("type") == t]


@pytest.fixture
def patched(monkeypatch):
    monkeypatch.setattr(planner_ws, "create_agent", lambda **kw: object())
    monkeypatch.setattr(planner_ws, "_get_langfuse", lambda: None)
    # Avoid DB writes from persistence in the turn-loop tests.
    monkeypatch.setattr(planner_ws, "_persist_session", lambda *a, **k: None)
    return monkeypatch


def _seed_conv(cid: str):
    planner_state.store_conv(cid, {
        "messages": [],
        "model": {"model_name": "qwen3.6-27b", "provider": "glm"},
        "memory": planner_state._new_memory(),
        "file_artifacts": [],
    })


def _proposal_block(extra: dict | None = None) -> str:
    proposal = {
        "ready": True,
        "proposal": {
            "title": "销售日报 Agent",
            "goal": "每天生成客户分层日报",
            "architecture_summary": "P → Agent ← M",
            "runtime_mode": "cron",
            "deliverables": ["日报"],
            "recommended_capabilities": [{"id": 1, "type": "tool", "name": "sql_query"}],
            "context_used_explanation": ["基于销售总监角色"],
            "nodes": [{"id": "agent1", "type": "agent", "config": {"system_prompt": "x"}}],
            "edges": [],
        },
    }
    if extra:
        proposal["proposal"].update(extra)
    return "```json\n" + json.dumps(proposal, ensure_ascii=False) + "\n```"


# ─── 5.3 schema / validation unit tests ─────────────────────────────────────

def test_proposal_payload_validates_and_caps_are_structured_refs():
    payload, ok = _validate_proposal_payload({
        "title": "t", "goal": "g", "runtime_mode": "cron",
        "recommended_capabilities": [{"id": 1, "type": "tool", "name": "sql_query"}],
        "deliverables": ["日报"], "context_used_explanation": ["x"],
    })
    assert ok and payload is not None
    rc = payload.recommended_capabilities[0]
    assert (rc.id, rc.type, rc.name) == (1, "tool", "sql_query")


def test_planner_intermediate_fields_present_with_defaults():
    pi = PlannerIntermediate(inferred_goal="生成日报 agent", task_type="analysis_report")
    # MUST-present fields all exist (schema completeness = the contract).
    for f in ("inferred_goal", "task_type", "confidence", "missing_critical_info",
              "expert_candidates", "capability_candidates", "recommended_runtime_mode",
              "clarification_required", "context_used_explanation", "proposal"):
        assert hasattr(pi, f)
    assert pi.expert_candidates == []  # empty placeholder at T2
    assert pi.clarification_required is False


def test_proposal_validation_fails_on_drift():
    # Non-dict proposal is genuine drift → ok False, degrade.
    _, ok = _validate_proposal_payload(["not", "a", "dict"])
    assert ok is False


# ─── capability heuristic (4.4) ─────────────────────────────────────────────

def test_capability_candidates_prefers_name_hits():
    ctx = {
        "user_input": {"goal_text": "我要用 sql_query 做分析"},
        "internal_context": {"capabilities": [
            {"id": 1, "type": "tool", "name": "sql_query"},
            {"id": 2, "type": "skill", "name": "report-generator"},
        ]},
    }
    cands = _match_capability_candidates(ctx, {"recommended_capabilities": []})
    assert cands[0]["name"] == "sql_query"  # hit ranked first
    assert all(set(c.keys()) == {"id", "type", "name"} for c in cands)


def test_capability_candidates_empty_without_capabilities():
    assert _match_capability_candidates({"internal_context": {"capabilities": []}}, {}) == []


# ─── context block render (3.1) — no raw content ────────────────────────────

def test_context_block_has_caps_but_no_raw_content():
    ctx = {
        "user_profile": {"role": "销售总监", "preferences": {"language": "zh-CN"}},
        "workspace_context": {"connected_systems": ["crm"]},
        "internal_context": {
            "capabilities": [{"id": 1, "type": "tool", "name": "sql_query"}],
            "runtime_modes": ["direct", "cron"],
        },
        "policy_context": {"clarification_policy": "minimal", "publish_policy": "draft_before_publish"},
    }
    block = _render_context_block(ctx)
    assert "规划上下文" in block
    assert "sql_query" in block and "销售总监" in block
    assert "RAW" not in block  # no config/content leakage by construction


# ─── 5.1 no-clarification turn yields a structured, persisted proposal ───────

@pytest.mark.asyncio
async def test_proposal_turn_validates_and_persists(patched):
    # Seed a platform capability so planning_context.internal_context carries it
    # (the heuristic matches against the platform pool, not the proposal alone).
    from sqlmodel import Session, select
    from app.models.db import CapabilityItem
    from app.api import planner as planner_api
    with Session(planner_api.engine) as s:
        if not s.exec(select(CapabilityItem).where(CapabilityItem.name == "sql_query")).first():
            s.add(CapabilityItem(type="tool", name="sql_query", description="d",
                                 tags="[]", config="{}"))
            s.commit()
    patched.setattr(planner_ws, "run_conversation", _script_stream([
        ("token", "依据你的角色与工作区，给出方案如下。"),
        ("token", _proposal_block()),
        ("done", ""),
    ]))
    _seed_conv("conv-prop")
    sock = FakeWebSocket([{"type": "message", "content": "做个销售日报 agent，信息我都给齐了"}])
    await planner_ws.planner_websocket(sock, "conv-prop")

    finals = _of_type(sock.sent, "final_summary")
    assert finals, "a structured proposal turn must emit final_summary"
    # capability_candidates derived from planning_context were attached.
    prop = finals[-1]["proposal"]
    assert any(c.get("name") == "sql_query" for c in prop.get("capability_candidates", []))
    # validated payload mirrored into persisted memory (no new table).
    conv = planner_state.load_conv("conv-prop")
    assert conv["memory"].get("proposal_payload", {}).get("title") == "销售日报 Agent"


# ─── 5.2 minimal-clarification turn (no proposal, asks a question) ───────────

@pytest.mark.asyncio
async def test_clarification_turn_has_no_proposal(patched):
    # A2UI-style minimal clarification: no proposal JSON this turn.
    a2ui = ("<a2ui_request>" + json.dumps({
        "id": "c1", "prompt": "你的日报面向谁？",
        "options": [{"id": "mgr", "label": "销售总监"}, {"id": "rep", "label": "一线销售"}],
    }, ensure_ascii=False) + "</a2ui_request>")
    patched.setattr(planner_ws, "run_conversation", _script_stream([
        ("token", "有一个关键点需要你确认。"),
        ("token", a2ui),
        ("done", ""),
    ]))
    _seed_conv("conv-clar")
    sock = FakeWebSocket([{"type": "message", "content": "帮我做个日报 agent"}])
    await planner_ws.planner_websocket(sock, "conv-clar")

    assert not _of_type(sock.sent, "final_summary"), "clarification turn must not emit a proposal"
    assert _of_type(sock.sent, "a2ui_request"), "clarification turn surfaces an a2ui card"


# ─── 5.4 validation-failure degrades without aborting ───────────────────────

@pytest.mark.asyncio
async def test_validation_failure_degrades_keeps_reply(patched, monkeypatch):
    # Force the validator to report drift; the turn must still complete with a
    # reply (done) and NOT raise/abort.
    monkeypatch.setattr(planner_ws, "_validate_proposal_payload", lambda p: (None, False))
    patched.setattr(planner_ws, "run_conversation", _script_stream([
        ("token", "这是给你的方案。"),
        ("token", _proposal_block()),
        ("done", ""),
    ]))
    _seed_conv("conv-drift")
    sock = FakeWebSocket([{"type": "message", "content": "做个 agent"}])
    await planner_ws.planner_websocket(sock, "conv-drift")

    assert _of_type(sock.sent, "done"), "turn completes despite validation failure"
    assert not _of_type(sock.sent, "error"), "validation failure must not surface as a turn error"
    # No structured mirror persisted on drift.
    conv = planner_state.load_conv("conv-drift")
    assert "proposal_payload" not in (conv["memory"] or {})


# ─── 5.5 feature flag falls back to legacy path ─────────────────────────────

@pytest.mark.asyncio
async def test_feature_flag_off_skips_planning_context(patched, monkeypatch):
    monkeypatch.setenv("PLANNER_PLANNING_CONTEXT", "off")

    captured = {}

    def _fake_build(**kwargs):
        captured["called"] = True
        return {}

    monkeypatch.setattr("app.core.planning_context.build_planning_context", _fake_build)
    patched.setattr(planner_ws, "run_conversation", _script_stream([
        ("token", "好的。"),
        ("done", ""),
    ]))
    _seed_conv("conv-flagoff")
    sock = FakeWebSocket([{"type": "message", "content": "你好"}])
    await planner_ws.planner_websocket(sock, "conv-flagoff")

    assert "called" not in captured, "flag off must not aggregate planning_context"
    conv = planner_state.load_conv("conv-flagoff")
    assert "planning_context" not in conv


@pytest.mark.asyncio
async def test_feature_flag_on_injects_planning_context(patched, monkeypatch):
    monkeypatch.setenv("PLANNER_PLANNING_CONTEXT", "on")
    patched.setattr(planner_ws, "run_conversation", _script_stream([
        ("token", "好的。"),
        ("done", ""),
    ]))
    _seed_conv("conv-flagon")
    sock = FakeWebSocket([{"type": "message", "content": "你好"}])
    await planner_ws.planner_websocket(sock, "conv-flagon")

    conv = planner_state.load_conv("conv-flagon")
    assert "planning_context" in conv, "flag on stashes the aggregated context"
    assert set(conv["planning_context"].keys()) >= {
        "user_input", "user_profile", "internal_context", "policy_context",
    }
