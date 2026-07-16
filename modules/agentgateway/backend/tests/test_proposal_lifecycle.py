"""Backend tests for change: proposal-draft-agent-lifecycle (T5).

Covers spec proposal-lifecycle + draft-agent-staging + the planner-proposal-flow
persistence delta:
- proposal persisted first-class (proposals table), re-readable, single active draft
- status machine: legal draft→confirmed/rejected, confirmed→applied; illegal rejected→applied
- proposal_states multi-round update
- proposal→draft_agent conversion: confirmed-only, idempotent, no Agent/DAGGraph
- read-only API shape; coexists with legacy apply / architecture_proposals
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.api import planner as planner_api
from app.core import proposal_store
from app.core.draft_agent import convert_proposal_to_draft
from app.models.db import Agent, DAGGraph, DraftAgent, Proposal, ProposalState


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(planner_api.router, prefix="/api")
    return TestClient(app)


def _payload(title="销售日报 Agent", runtime_mode="cron"):
    return {
        "title": title, "goal": "每天生成客户分层日报", "runtime_mode": runtime_mode,
        "deliverables": ["日报"],
        "recommended_capabilities": [{"id": 1, "type": "tool", "name": "sql_query"}],
        "primary_expert_template_id": 7,
    }


def _seed_proposal(cid="conv-t5", **kw):
    return proposal_store.upsert_proposal(
        conversation_id=cid, user_goal="做个日报 agent",
        inferred_goal="销售日报", task_type="analysis_report",
        proposal_json=json.dumps(_payload(**kw), ensure_ascii=False),
        selected_runtime_mode="cron", selected_expert_template_id=7,
    )


# ─── 6.1 first-class persistence ────────────────────────────────────────────

def test_proposal_persisted_and_readable():
    p = _seed_proposal(cid="conv-persist")
    assert p.id is not None and p.status == "draft"
    got = proposal_store.get_proposal_by_conversation("conv-persist")
    assert got is not None and got.id == p.id
    assert json.loads(got.proposal_json)["title"] == "销售日报 Agent"


def test_single_active_draft_updates_not_stacks():
    p1 = _seed_proposal(cid="conv-single", title="V1")
    p2 = _seed_proposal(cid="conv-single", title="V2")
    assert p1.id == p2.id  # same active draft updated
    with Session(planner_api.engine) as s:
        drafts = s.exec(select(Proposal).where(
            Proposal.conversation_id == "conv-single", Proposal.status == "draft"
        )).all()
    assert len(drafts) == 1
    assert json.loads(drafts[0].proposal_json)["title"] == "V2"


# ─── 6.2 status machine ─────────────────────────────────────────────────────

def test_legal_transitions():
    p = _seed_proposal(cid="conv-legal")
    assert proposal_store.transition_status(p.id, "confirmed") is True
    assert proposal_store.transition_status(p.id, "applied") is True
    assert proposal_store.get_proposal(p.id).status == "applied"


def test_illegal_transition_rejected():
    p = _seed_proposal(cid="conv-illegal")
    assert proposal_store.transition_status(p.id, "rejected") is True
    # rejected is terminal — cannot jump to applied
    assert proposal_store.transition_status(p.id, "applied") is False
    assert proposal_store.get_proposal(p.id).status == "rejected"


def test_draft_cannot_skip_to_applied():
    p = _seed_proposal(cid="conv-skip")
    assert proposal_store.transition_status(p.id, "applied") is False
    assert proposal_store.get_proposal(p.id).status == "draft"


# ─── 6.3 proposal_states multi-round ────────────────────────────────────────

def test_proposal_state_multi_round():
    p = _seed_proposal(cid="conv-state")
    proposal_store.upsert_proposal_state(p.id, selected_option="optA",
                                         confirmed_constraints=["只读 CRM"])
    proposal_store.upsert_proposal_state(p.id, rejected_options=["optB"],
                                         latest_summary="用户拒绝 B 选 A")
    st = proposal_store.get_proposal_state(p.id)
    assert st.selected_option == "optA"  # preserved across rounds
    assert json.loads(st.rejected_options_json) == ["optB"]
    assert json.loads(st.confirmed_constraints_json) == ["只读 CRM"]
    assert st.latest_summary == "用户拒绝 B 选 A"
    with Session(planner_api.engine) as s:
        rows = s.exec(select(ProposalState).where(ProposalState.proposal_id == p.id)).all()
    assert len(rows) == 1  # one row per proposal (upsert)


# ─── 6.4 conversion ─────────────────────────────────────────────────────────

def test_convert_requires_confirmed():
    p = _seed_proposal(cid="conv-conv1")
    assert convert_proposal_to_draft(p.id) is None  # draft → refused
    proposal_store.transition_status(p.id, "confirmed")
    draft = convert_proposal_to_draft(p.id)
    assert draft is not None
    assert draft.runtime_mode == "cron"
    refs = json.loads(draft.capability_refs_json)
    assert refs and refs[0]["name"] == "sql_query"
    assert "RAW" not in draft.config_json  # structured only


def test_convert_idempotent_no_agent_rows():
    p = _seed_proposal(cid="conv-conv2")
    proposal_store.transition_status(p.id, "confirmed")
    d1 = convert_proposal_to_draft(p.id)
    d2 = convert_proposal_to_draft(p.id)
    assert d1.id == d2.id  # idempotent
    with Session(planner_api.engine) as s:
        drafts = s.exec(select(DraftAgent).where(DraftAgent.proposal_id == p.id)).all()
        assert len(drafts) == 1
        # conversion must NOT create production Agent / DAGGraph rows
        assert s.exec(select(Agent).where(Agent.name == "销售日报 Agent")).first() is None


# ─── 6.5 read-only + transition API ─────────────────────────────────────────

def test_proposal_api_and_transition(client: TestClient):
    _seed_proposal(cid="conv-api")
    resp = client.get("/api/planner/proposals/conv-api")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "draft" and body["proposal"]["title"] == "销售日报 Agent"
    pid = body["id"]
    # confirm via transition endpoint → draft agent derived
    r2 = client.post(f"/api/planner/proposals/by-id/{pid}/transition", json={"action": "confirm"})
    assert r2.status_code == 200
    assert r2.json()["status"] == "confirmed"
    assert r2.json()["draft_agent"] is not None
    # illegal transition → 409
    r3 = client.post(f"/api/planner/proposals/by-id/{pid}/transition", json={"action": "reject"})
    assert r3.status_code == 409


# ─── 6.6 coexistence with legacy apply / architecture_proposals ─────────────

def test_proposals_table_distinct_from_architecture_proposals():
    from app.models.db import ArchitectureProposal
    with Session(planner_api.engine) as s:
        arch_before = len(s.exec(select(ArchitectureProposal)).all())
    _seed_proposal(cid="conv-coexist")
    with Session(planner_api.engine) as s:
        props = s.exec(select(Proposal).where(Proposal.conversation_id == "conv-coexist")).all()
        arch_after = len(s.exec(select(ArchitectureProposal)).all())
    # a first-class proposal lands in proposals only — architecture_proposals
    # (the legacy post-apply archive) is untouched by this path.
    assert len(props) == 1
    assert arch_after == arch_before
