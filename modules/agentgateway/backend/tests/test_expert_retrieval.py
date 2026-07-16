"""Backend tests for change: expert-template-ingestion-and-retrieval (T3).

Covers spec expert-template-retrieval + the planning-context-aggregation delta:
- expert retrieval ranks relevant domain first, empty on no match, no raw content
- internal_context splits expert_template rows into expert_templates (not capabilities)
- planner fills expert_candidates + primary_expert_template_id on a proposal turn
- sample seed is idempotent
- read-only retrieval API shape, no write side effects
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.api import planner as planner_api
from app.api.seed import seed_expert_templates, SEED_EXPERT_TEMPLATES
from app.core import planning_context as pc
from app.core.expert_retrieval import retrieve_expert_candidates
from app.models.db import CapabilityItem


def _seed_template(name: str, domain: str, *, deliverables=None, tags=None,
                   persona="SECRET PERSONA TEXT"):
    with Session(planner_api.engine) as s:
        if s.exec(select(CapabilityItem).where(
            CapabilityItem.type == "expert_template", CapabilityItem.name == name
        )).first():
            return
        s.add(CapabilityItem(
            type="expert_template", name=name, description="d",
            tags=json.dumps(tags or [], ensure_ascii=False),
            config=json.dumps({
                "domain": domain,
                "deliverables": deliverables or [],
                "persona_summary": persona,
            }, ensure_ascii=False),
        ))
        s.commit()


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(planner_api.router, prefix="/api")
    return TestClient(app)


# ─── 7.1 retrieval ranking / empty / no-raw ─────────────────────────────────

def test_retrieval_ranks_relevant_domain_first():
    _seed_template("sales-x", "sales", deliverables=["销售日报"], tags=["sales"])
    _seed_template("legal-x", "legal", deliverables=["合同审查"], tags=["legal"])
    cands = retrieve_expert_candidates(goal_text="帮我做一个 sales 日报 agent")
    assert cands, "expected at least one candidate"
    assert cands[0]["domain"] == "sales"
    scores = [c["score"] for c in cands]
    assert scores == sorted(scores, reverse=True) and scores[0] > 0


def test_retrieval_empty_on_no_match():
    cands = retrieve_expert_candidates(goal_text="量子色动力学模拟")
    assert cands == []


def test_retrieval_candidates_have_no_raw_content():
    _seed_template("sales-y", "sales", deliverables=["日报"], tags=["sales"],
                   persona="SECRET PERSONA TEXT")
    cands = retrieve_expert_candidates(goal_text="sales 日报")
    assert cands
    for c in cands:
        # T4 added recommended_capability_tags / recommended_modes (structured,
        # still no raw content). Core T3 keys remain a subset.
        assert {"id", "name", "domain", "score"} <= set(c.keys())
    assert "SECRET PERSONA TEXT" not in json.dumps(cands)


def test_domain_hint_weights_retrieval():
    _seed_template("sales-z", "sales", tags=["sales"])
    cands = retrieve_expert_candidates(goal_text="", domain_hint="sales")
    assert any(c["domain"] == "sales" for c in cands)


# ─── 7.2 internal_context split ─────────────────────────────────────────────

def test_internal_context_splits_expert_templates_out_of_capabilities():
    _seed_template("split-expert", "sales", tags=["sales"])
    with Session(planner_api.engine) as s:
        if not s.exec(select(CapabilityItem).where(CapabilityItem.name == "split-tool")).first():
            s.add(CapabilityItem(type="tool", name="split-tool", description="d",
                                 tags="[]", config="{}"))
            s.commit()
    ctx = pc.build_planning_context(user_input={"goal_text": "g"})
    internal = ctx["internal_context"]
    cap_names = [c["name"] for c in internal["capabilities"]]
    et_names = [e["name"] for e in internal["expert_templates"]]
    assert "split-expert" in et_names, "expert_template must appear in expert_templates"
    assert "split-expert" not in cap_names, "expert_template must NOT leak into capabilities"
    assert "split-tool" in cap_names, "normal capability stays in capabilities"
    e = next(e for e in internal["expert_templates"] if e["name"] == "split-expert")
    assert e["domain"] == "sales" and "id" in e


# ─── 7.4 sample seed idempotency ────────────────────────────────────────────

def test_sample_seed_is_idempotent():
    seed_expert_templates()
    seed_expert_templates()
    with Session(planner_api.engine) as s:
        for tpl in SEED_EXPERT_TEMPLATES:
            rows = s.exec(select(CapabilityItem).where(
                CapabilityItem.type == "expert_template",
                CapabilityItem.name == tpl["name"],
            )).all()
            assert len(rows) == 1, f"{tpl['name']} must be seeded exactly once"


# ─── 7.5 read-only API ──────────────────────────────────────────────────────

def test_retrieve_api_returns_candidates(client: TestClient):
    seed_expert_templates()
    resp = client.get("/api/planner/expert-templates/retrieve",
                      params={"goal": "做一个 sales 销售管道分析 agent"})
    assert resp.status_code == 200
    body = resp.json()
    assert "expert_candidates" in body
    assert any(c["domain"] == "sales" for c in body["expert_candidates"])


def test_retrieve_api_no_write_side_effect(client: TestClient):
    with Session(planner_api.engine) as s:
        before = len(s.exec(select(CapabilityItem)).all())
    client.get("/api/planner/expert-templates/retrieve", params={"goal": "x"})
    with Session(planner_api.engine) as s:
        after = len(s.exec(select(CapabilityItem)).all())
    assert after == before


# ─── 7.3 planner integration ────────────────────────────────────────────────

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


def _proposal_block() -> str:
    proposal = {
        "ready": True,
        "proposal": {
            "title": "销售日报 Agent", "goal": "每天生成客户分层日报",
            "architecture_summary": "P → Agent ← M", "runtime_mode": "cron",
            "deliverables": ["日报"], "recommended_capabilities": [],
            "context_used_explanation": [], "nodes": [], "edges": [],
        },
    }
    return "```json\n" + json.dumps(proposal, ensure_ascii=False) + "\n```"


@pytest.mark.asyncio
async def test_planner_fills_expert_candidates_on_proposal(monkeypatch):
    from app.api.planner import ws as planner_ws
    from app.api.planner import state as planner_state
    monkeypatch.setattr(planner_ws, "create_agent", lambda **kw: object())
    monkeypatch.setattr(planner_ws, "_get_langfuse", lambda: None)
    monkeypatch.setattr(planner_ws, "_persist_session", lambda *a, **k: None)
    monkeypatch.setattr(planner_ws, "run_conversation", _script_stream([
        ("token", "依据你的需求给出方案。"),
        ("token", _proposal_block()),
        ("done", ""),
    ]))
    _seed_template("sales-pipeline-analyst", "sales",
                   deliverables=["销售日报"], tags=["sales", "analysis"])
    planner_state.store_conv("conv-et", {
        "messages": [], "model": {"model_name": "qwen3.6-27b", "provider": "glm"},
        "memory": planner_state._new_memory(), "file_artifacts": [],
    })
    sock = FakeWebSocket([{"type": "message", "content": "做个 sales 销售管道分析日报 agent"}])
    await planner_ws.planner_websocket(sock, "conv-et")

    finals = [m for m in sock.sent if m.get("type") == "final_summary"]
    assert finals, "proposal turn must emit final_summary"
    prop = finals[-1]["proposal"]
    assert prop.get("expert_candidates"), "expert_candidates must be filled"
    assert prop.get("primary_expert_template_id") is not None
    assert any("专家模板" in str(x) for x in prop.get("context_used_explanation", []))
