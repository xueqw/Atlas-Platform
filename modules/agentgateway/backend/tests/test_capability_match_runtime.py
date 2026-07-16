"""Backend tests for change: capability-match-and-runtime-mode (T4).

Covers spec capability-match-and-runtime-mode:
- tag/expert-template aware capability match (ranking, structured-only, empty)
- recommended_capabilities back-fill (fills when absent, never overwrites)
- runtime_mode recommendation (keyword + expert modes, legal set)
- planner-first runtime_mode (explicit value preserved, recommended for reference)
- best-effort: matcher failure does not abort the proposal turn
"""

from __future__ import annotations

import json

import pytest
from sqlmodel import Session, select

from app.api import planner as planner_api
from app.api.planner.proposal import (
    _match_capability_candidates,
    recommend_runtime_mode,
    _RUNTIME_MODES,
)
from app.models.db import CapabilityItem


def _seed_cap(name: str, ctype: str = "tool", tags=None):
    with Session(planner_api.engine) as s:
        if s.exec(select(CapabilityItem).where(CapabilityItem.name == name)).first():
            return
        s.add(CapabilityItem(type=ctype, name=name, description="d",
                             tags=json.dumps(tags or [], ensure_ascii=False), config="{}"))
        s.commit()


def _ctx(goal: str, cap_refs):
    return {
        "user_input": {"goal_text": goal},
        "internal_context": {"capabilities": cap_refs},
    }


def _cap_ref(name: str, ctype: str = "tool"):
    with Session(planner_api.engine) as s:
        row = s.exec(select(CapabilityItem).where(CapabilityItem.name == name)).first()
        return {"id": row.id, "type": row.type, "name": row.name}


# ─── 5.1 matcher ────────────────────────────────────────────────────────────

def test_expert_tags_boost_ranking():
    _seed_cap("crm-aggregator", "tool", tags=["sales", "analysis"])
    _seed_cap("image-gen", "tool", tags=["media"])
    refs = [_cap_ref("crm-aggregator"), _cap_ref("image-gen")]
    # goal text mentions neither name; ranking must come from expert tag overlap.
    with Session(planner_api.engine) as s:
        cands = _match_capability_candidates(
            _ctx("帮我做点东西", refs), {"recommended_capabilities": []},
            expert_tags=["sales", "analysis"], session=s,
        )
    assert cands[0]["name"] == "crm-aggregator"
    assert all(set(c.keys()) == {"id", "type", "name"} for c in cands)


def test_matcher_empty_without_capabilities():
    assert _match_capability_candidates(
        {"internal_context": {"capabilities": []}}, {}, session=None
    ) == []


def test_matcher_name_only_degrades_without_session():
    # Backward-compatible with T2: no session → name-overlap only, still works.
    _seed_cap("sql_query", "tool", tags=["data"])
    refs = [_cap_ref("sql_query")]
    cands = _match_capability_candidates(
        _ctx("我要用 sql_query 分析", refs), {"recommended_capabilities": []},
    )
    assert cands and cands[0]["name"] == "sql_query"


# ─── 5.3 runtime mode recommendation ────────────────────────────────────────

def test_runtime_mode_cron_keyword():
    assert recommend_runtime_mode(goal_text="每天定时生成销售日报") == "cron"


def test_runtime_mode_flow_keyword():
    assert recommend_runtime_mode(goal_text="一个多步审批工作流") == "flow"


def test_runtime_mode_multi_agent_keyword():
    assert recommend_runtime_mode(goal_text="多代理协同处理") == "multi_agent"


def test_runtime_mode_from_expert_modes():
    # No keyword signal → fall back to matched template's first valid mode.
    assert recommend_runtime_mode(goal_text="做个助手", expert_modes=["cron", "flow"]) == "cron"


def test_runtime_mode_default_and_legal_set():
    m = recommend_runtime_mode(goal_text="随便做个东西")
    assert m == "direct"
    assert m in _RUNTIME_MODES


# ─── 5.2 + 5.4 + 5.5 turn-loop integration ──────────────────────────────────

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


def _proposal_block(extra=None):
    proposal = {
        "ready": True,
        "proposal": {
            "title": "销售日报 Agent", "goal": "每天生成客户分层日报",
            "architecture_summary": "P → Agent ← M",
            "deliverables": ["日报"], "recommended_capabilities": [],
            "context_used_explanation": [], "nodes": [], "edges": [],
        },
    }
    if extra:
        proposal["proposal"].update(extra)
    return "```json\n" + json.dumps(proposal, ensure_ascii=False) + "\n```"


def _seed_conv(cid):
    from app.api.planner import state as planner_state
    planner_state.store_conv(cid, {
        "messages": [], "model": {"model_name": "qwen3.6-27b", "provider": "glm"},
        "memory": planner_state._new_memory(), "file_artifacts": [],
    })


@pytest.fixture
def patched(monkeypatch):
    from app.api.planner import ws as planner_ws
    monkeypatch.setattr(planner_ws, "create_agent", lambda **kw: object())
    monkeypatch.setattr(planner_ws, "_get_langfuse", lambda: None)
    monkeypatch.setattr(planner_ws, "_persist_session", lambda *a, **k: None)
    return monkeypatch


@pytest.mark.asyncio
async def test_turn_fills_recommended_caps_and_runtime_mode(patched):
    from app.api.planner import ws as planner_ws
    _seed_cap("crm-aggregator", "tool", tags=["sales", "analysis"])
    patched.setattr(planner_ws, "run_conversation", _script_stream([
        ("token", "依据你的需求给出方案。"),
        # proposal omits runtime_mode → recommender should fill it (cron from goal).
        ("token", _proposal_block()),
        ("done", ""),
    ]))
    _seed_conv("conv-t4")
    sock = FakeWebSocket([{"type": "message", "content": "每天定时生成 sales 销售日报 agent"}])
    await planner_ws.planner_websocket(sock, "conv-t4")

    finals = [m for m in sock.sent if m.get("type") == "final_summary"]
    assert finals
    prop = finals[-1]["proposal"]
    assert prop.get("recommended_runtime_mode") == "cron"
    assert prop.get("runtime_mode") == "cron"  # back-filled (proposal left it empty)
    # recommended_capabilities back-filled from candidates (proposal gave none)
    assert prop.get("recommended_capabilities"), "recommended_capabilities must be back-filled"


@pytest.mark.asyncio
async def test_turn_preserves_planner_runtime_mode(patched):
    from app.api.planner import ws as planner_ws
    patched.setattr(planner_ws, "run_conversation", _script_stream([
        ("token", "方案如下。"),
        # planner explicitly chose flow; recommender would say cron — must NOT override.
        ("token", _proposal_block({"runtime_mode": "flow",
                                   "recommended_capabilities": [{"id": 1, "type": "tool", "name": "x"}]})),
        ("done", ""),
    ]))
    _seed_conv("conv-t4b")
    sock = FakeWebSocket([{"type": "message", "content": "每天定时日报"}])
    await planner_ws.planner_websocket(sock, "conv-t4b")

    prop = [m for m in sock.sent if m.get("type") == "final_summary"][-1]["proposal"]
    assert prop["runtime_mode"] == "flow", "planner's explicit runtime_mode must be preserved"
    assert prop["recommended_runtime_mode"] == "cron", "recommendation still recorded for reference"
    # planner's explicit recommended_capabilities preserved (not overwritten);
    # a status marker (available / to_create) may be ADDED by the library check.
    rc = prop["recommended_capabilities"]
    assert len(rc) == 1
    assert rc[0]["id"] == 1 and rc[0]["type"] == "tool" and rc[0]["name"] == "x"


@pytest.mark.asyncio
async def test_turn_degrades_when_matcher_raises(patched, monkeypatch):
    from app.api.planner import ws as planner_ws
    monkeypatch.setattr(planner_ws, "_match_capability_candidates",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    patched.setattr(planner_ws, "run_conversation", _script_stream([
        ("token", "方案。"),
        ("token", _proposal_block()),
        ("done", ""),
    ]))
    _seed_conv("conv-t4c")
    sock = FakeWebSocket([{"type": "message", "content": "做个 agent"}])
    await planner_ws.planner_websocket(sock, "conv-t4c")

    assert [m for m in sock.sent if m.get("type") == "done"], "turn completes despite matcher failure"
    assert not [m for m in sock.sent if m.get("type") == "error"]
