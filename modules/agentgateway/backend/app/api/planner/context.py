"""Read-only Planning Context endpoint (T1: planning-context-aggregation).

Exposes the aggregated ``planning_context`` for a planning turn so the frontend
/ debugging tools can inspect it. Real-time aggregation, no write side effects
(design D4). Writes still flow through the WebSocket turn loop.
"""

from typing import Optional

import json

from fastapi import Query

from app.core.planning_context import build_planning_context
from app.core.expert_retrieval import retrieve_expert_candidates


def get_planning_context(
    conversation_id: Optional[str] = Query(None),
    user_id: Optional[str] = Query(None),
    workspace_id: Optional[str] = Query(None),
    goal_text: Optional[str] = Query(None),
) -> dict:
    """Return ``{"planning_context": {...}}`` with all seven segments present.

    Pure read: aggregates from the source tables on each call. ``goal_text`` is
    accepted as a query param so callers can preview the context for a draft
    goal without starting a turn."""
    user_input = {"goal_text": goal_text} if goal_text else None
    ctx = build_planning_context(
        conversation_id=conversation_id,
        user_input=user_input,
        user_id=user_id,
        workspace_id=workspace_id,
    )
    return {"planning_context": ctx}


def retrieve_expert_templates(
    goal: Optional[str] = Query(None),
    domain: Optional[str] = Query(None),
    limit: int = Query(5),
) -> dict:
    """Read-only expert-template retrieval (T3). Returns top-k expert candidates
    as structured refs (id/name/domain/score) — no persona / raw content. Pure
    read: scores ``expert_template`` capabilities against the goal/domain on each
    call. Expert-template CRUD lives on the existing /capabilities API."""
    cands = retrieve_expert_candidates(
        goal_text=goal or "",
        domain_hint=domain or "",
        limit=limit,
    )
    return {"expert_candidates": cands}


def list_asset_ingest_runs(limit: int = Query(20)) -> dict:
    """Read-only: recent asset ingest runs (T10 observability). No side effects."""
    from sqlmodel import Session, select, desc
    from app.core.database import engine
    from app.models.db import AssetIngestRun
    with Session(engine) as s:
        rows = s.exec(
            select(AssetIngestRun).order_by(desc(AssetIngestRun.created_at)).limit(limit)
        ).all()
        runs = [{
            "id": r.id, "source_name": r.source_name, "source_repo": r.source_repo,
            "status": r.status, "source_version": r.source_version,
            "manifest_key": r.manifest_key,
            "summary": json.loads(r.summary_json or "{}"),
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "completed_at": r.completed_at.isoformat() if r.completed_at else None,
        } for r in rows]
    return {"runs": runs}


def list_asset_ingest_events(run_id: int, limit: int = Query(500)) -> dict:
    """Read-only: per-stage events for one ingest run (T10). No side effects."""
    from sqlmodel import Session, select
    from app.core.database import engine
    from app.models.db import AssetEvent
    with Session(engine) as s:
        rows = s.exec(
            select(AssetEvent).where(AssetEvent.ingest_run_id == run_id)
            .order_by(AssetEvent.id).limit(limit)
        ).all()
        events = [{
            "id": e.id, "event_type": e.event_type, "status": e.status,
            "source_path": e.source_path, "object_key": e.object_key,
            "asset_type": e.asset_type, "message": e.message,
            "created_at": e.created_at.isoformat() if e.created_at else None,
        } for e in rows]
    return {"run_id": run_id, "events": events}


# ── Proposal lifecycle (T5) ──────────────────────────────────────────────────

def _proposal_view(p, state, draft) -> dict:
    return {
        "id": p.id, "conversation_id": p.conversation_id, "status": p.status,
        "user_goal": p.user_goal, "inferred_goal": p.inferred_goal,
        "task_type": p.task_type, "selected_runtime_mode": p.selected_runtime_mode,
        "selected_expert_template_id": p.selected_expert_template_id,
        "proposal": json.loads(p.proposal_json or "{}"),
        "state": None if state is None else {
            "selected_option": state.selected_option,
            "rejected_options": json.loads(state.rejected_options_json or "[]"),
            "confirmed_constraints": json.loads(state.confirmed_constraints_json or "[]"),
            "clarification_answers": json.loads(state.clarification_answers_json or "[]"),
            "latest_summary": state.latest_summary,
        },
        "draft_agent": None if draft is None else {
            "id": draft.id, "name": draft.name, "status": draft.status,
            "runtime_mode": draft.runtime_mode,
            "config": json.loads(draft.config_json or "{}"),
            "capability_refs": json.loads(draft.capability_refs_json or "[]"),
        },
    }


def get_proposal_by_conversation(conversation_id: str) -> dict:
    """Read-only: most recent proposal (+ state + draft agent) for a conversation."""
    from app.core import proposal_store
    from app.core.draft_agent import get_draft_agent_by_proposal
    p = proposal_store.get_proposal_by_conversation(conversation_id)
    if p is None:
        return {"proposal": None}
    state = proposal_store.get_proposal_state(p.id)
    draft = get_draft_agent_by_proposal(p.id)
    return _proposal_view(p, state, draft)


def get_proposal_by_id(proposal_id: int) -> dict:
    """Read-only: one proposal (+ state + draft agent) by id."""
    from app.core import proposal_store
    from app.core.draft_agent import get_draft_agent_by_proposal
    p = proposal_store.get_proposal(proposal_id)
    if p is None:
        return {"proposal": None}
    state = proposal_store.get_proposal_state(p.id)
    draft = get_draft_agent_by_proposal(p.id)
    return _proposal_view(p, state, draft)


def transition_proposal(proposal_id: int, body: dict) -> dict:
    """Drive the proposal state machine (T5). ``body.action`` ∈ {confirm, reject}.

    On a successful ``confirm`` the proposal moves draft→confirmed and a draft
    agent is derived (convert_proposal_to_draft). This is the minimal trigger so
    the lifecycle is exercisable from scripts/tests; the full UI flow is T7.
    Returns the refreshed proposal view; HTTP 409 on an illegal transition."""
    from fastapi import HTTPException
    from app.core import proposal_store
    from app.core.draft_agent import convert_proposal_to_draft, get_draft_agent_by_proposal

    action = str((body or {}).get("action") or "").lower()
    target = {"confirm": "confirmed", "reject": "rejected"}.get(action)
    if target is None:
        raise HTTPException(status_code=400, detail="action must be 'confirm' or 'reject'")
    ok = proposal_store.transition_status(proposal_id, target)
    if not ok:
        raise HTTPException(status_code=409, detail="illegal status transition")
    draft = None
    if target == "confirmed":
        draft = convert_proposal_to_draft(proposal_id)
    p = proposal_store.get_proposal(proposal_id)
    state = proposal_store.get_proposal_state(proposal_id)
    if draft is None:
        draft = get_draft_agent_by_proposal(proposal_id)
    return _proposal_view(p, state, draft)


# ── Draft Agent compile + test (T6) ──────────────────────────────────────────

def _draft_view(d) -> dict:
    return {
        "id": d.id, "proposal_id": d.proposal_id, "name": d.name, "status": d.status,
        "runtime_mode": d.runtime_mode,
        "config": json.loads(d.config_json or "{}"),
        "capability_refs": json.loads(d.capability_refs_json or "[]"),
    }


def get_draft_agent(draft_agent_id: int) -> dict:
    """Read-only: one draft agent by id (T6)."""
    from app.core.draft_agent import get_draft_agent as _get
    d = _get(draft_agent_id)
    return {"draft_agent": None if d is None else _draft_view(d)}


def compile_draft_agent_endpoint(draft_agent_id: int, body: dict = None) -> dict:
    """Compile + (optional) dry-run a draft agent (T6). Returns the structured
    CompileResult; never creates a production Agent. draft→tested on clean compile."""
    from app.core.draft_compile import compile_draft_agent
    run_dryrun = True
    if isinstance(body, dict) and "dry_run" in body:
        run_dryrun = bool(body["dry_run"])
    result = compile_draft_agent(draft_agent_id, run_dryrun=run_dryrun)
    return result


# ── Planner run events (T8) ──────────────────────────────────────────────────

def get_run_events(run_id: str) -> dict:
    """Read-only: the normalized stage timeline of one planner run (T8)."""
    from app.core.run_events import list_run_events
    return {"run_id": run_id, "events": list_run_events(run_id)}


def get_run_events_by_proposal(proposal_id: int) -> dict:
    """Read-only: run events linked to a proposal (T8)."""
    from app.core.run_events import list_run_events_by_proposal
    return {"proposal_id": proposal_id, "events": list_run_events_by_proposal(proposal_id)}


# ── Memory observability (T9) ────────────────────────────────────────────────

def list_memory_events_by_run(run_id: str, limit: int = Query(500)) -> dict:
    """Read-only: memory events for a planner run (T9)."""
    from app.core.memory_events import list_memory_events
    return {"run_id": run_id, "events": list_memory_events(run_id, limit=limit)}


def list_memory_events_for_proposal(proposal_id: int, limit: int = Query(500)) -> dict:
    """Read-only: memory events linked to a proposal (T9)."""
    from app.core.memory_events import list_memory_events_by_proposal
    return {"proposal_id": proposal_id, "events": list_memory_events_by_proposal(proposal_id, limit=limit)}


def audit_memory_items(
    agent_id: Optional[int] = Query(None),
    scope: Optional[str] = Query(None),
    limit: int = Query(100),
) -> dict:
    """Read-only memory audit (T9): what's stored — type/scope/source/summary/
    whether it has been used (access_count) — to verify '写进去的是什么'."""
    from sqlmodel import Session, select, desc
    from app.core.database import engine
    from app.models.db import MemoryItem
    with Session(engine) as s:
        stmt = select(MemoryItem).where(MemoryItem.status == "active")
        if agent_id is not None:
            stmt = stmt.where(MemoryItem.agent_id == agent_id)
        if scope:
            stmt = stmt.where(MemoryItem.scope == scope)
        rows = s.exec(stmt.order_by(desc(MemoryItem.updated_at)).limit(limit)).all()
        items = [{
            "id": r.id, "memory_type": r.memory_type, "scope": r.scope,
            "source_kind": r.source_kind, "source_ref": r.source_ref,
            "summary": r.summary, "importance": r.importance,
            "mem0_ref": r.mem0_ref, "access_count": r.access_count,
            "used": (r.access_count or 0) > 0,
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
        } for r in rows]
    return {"items": items}
