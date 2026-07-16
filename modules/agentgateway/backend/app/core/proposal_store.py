"""Proposal lifecycle store (T5: proposal-draft-agent-lifecycle).

First-class persistence + state machine for planner proposals (design §6.8-6.9).
Centralizes all reads/writes of ``proposals`` / ``proposal_states`` so the WS
turn loop, the read-only API, the conversion service, and tests share one path.

DISTINCT from the legacy ArchitectureProposal (post-apply archive) — these never
touch the existing apply flow.
"""

from __future__ import annotations

import json
import logging
from typing import List, Optional

from sqlmodel import Session, select, desc

from app.core.database import engine
from app.models.db import Proposal, ProposalState, _utcnow

_log = logging.getLogger(__name__)

# Legal status transitions (design D6). Confirm before applying; reject is terminal.
_TRANSITIONS = {
    "draft": {"confirmed", "rejected"},
    "confirmed": {"applied"},
    "rejected": set(),
    "applied": set(),
}


def _dumps(v) -> str:
    try:
        return json.dumps(v if v is not None else [], ensure_ascii=False)
    except (TypeError, ValueError):
        return "[]"


def upsert_proposal(
    *,
    conversation_id: str,
    user_goal: str = "",
    inferred_goal: str = "",
    task_type: str = "",
    proposal_json: str = "{}",
    selected_runtime_mode: str = "",
    selected_expert_template_id: Optional[str] = None,
    session: Optional[Session] = None,
) -> Proposal:
    """Create or update the active draft proposal for a conversation (D4).

    If the conversation already has a ``draft`` proposal, update it in place
    (single active draft); otherwise insert a new one. Confirmed/rejected history
    rows are left untouched (re-proposing after a reject starts a fresh draft)."""
    own = session is None
    s = session or Session(engine)
    try:
        row = s.exec(
            select(Proposal)
            .where(Proposal.conversation_id == conversation_id, Proposal.status == "draft")
            .order_by(desc(Proposal.updated_at))
        ).first()
        if row is None:
            row = Proposal(conversation_id=conversation_id, status="draft")
            s.add(row)
        row.user_goal = user_goal or row.user_goal
        row.inferred_goal = inferred_goal or row.inferred_goal
        row.task_type = task_type or row.task_type
        row.proposal_json = proposal_json or row.proposal_json
        row.selected_runtime_mode = selected_runtime_mode or row.selected_runtime_mode
        if selected_expert_template_id is not None:
            row.selected_expert_template_id = str(selected_expert_template_id)
        row.updated_at = _utcnow()
        s.add(row)
        s.commit()
        s.refresh(row)
        return row
    finally:
        if own:
            s.close()


def transition_status(proposal_id: int, target: str, *, session: Optional[Session] = None) -> bool:
    """Move a proposal to ``target`` if the transition is legal (D6). Returns
    False (no change) on illegal transition or missing proposal."""
    own = session is None
    s = session or Session(engine)
    try:
        row = s.get(Proposal, proposal_id)
        if row is None:
            return False
        if target not in _TRANSITIONS.get(row.status, set()):
            _log.info("rejected proposal transition %s -> %s (id=%s)", row.status, target, proposal_id)
            return False
        row.status = target
        row.updated_at = _utcnow()
        s.add(row)
        s.commit()
        return True
    finally:
        if own:
            s.close()


def upsert_proposal_state(
    proposal_id: int,
    *,
    selected_option: Optional[str] = None,
    rejected_options: Optional[List] = None,
    confirmed_constraints: Optional[List] = None,
    clarification_answers: Optional[List] = None,
    latest_summary: Optional[str] = None,
    session: Optional[Session] = None,
) -> ProposalState:
    """Create/update the single per-proposal interaction-state row (D2). Only
    provided fields are written; omitted fields keep their stored value."""
    own = session is None
    s = session or Session(engine)
    try:
        row = s.exec(select(ProposalState).where(ProposalState.proposal_id == proposal_id)).first()
        if row is None:
            row = ProposalState(proposal_id=proposal_id)
            s.add(row)
        if selected_option is not None:
            row.selected_option = selected_option
        if rejected_options is not None:
            row.rejected_options_json = _dumps(rejected_options)
        if confirmed_constraints is not None:
            row.confirmed_constraints_json = _dumps(confirmed_constraints)
        if clarification_answers is not None:
            row.clarification_answers_json = _dumps(clarification_answers)
        if latest_summary is not None:
            row.latest_summary = latest_summary
        row.updated_at = _utcnow()
        s.add(row)
        s.commit()
        s.refresh(row)
        return row
    finally:
        if own:
            s.close()


def get_proposal(proposal_id: int, *, session: Optional[Session] = None) -> Optional[Proposal]:
    own = session is None
    s = session or Session(engine)
    try:
        return s.get(Proposal, proposal_id)
    finally:
        if own:
            s.close()


def get_proposal_by_conversation(conversation_id: str, *, session: Optional[Session] = None) -> Optional[Proposal]:
    """Most-recently-updated proposal for a conversation (any status)."""
    own = session is None
    s = session or Session(engine)
    try:
        return s.exec(
            select(Proposal)
            .where(Proposal.conversation_id == conversation_id)
            .order_by(desc(Proposal.updated_at))
        ).first()
    finally:
        if own:
            s.close()


def get_proposal_state(proposal_id: int, *, session: Optional[Session] = None) -> Optional[ProposalState]:
    own = session is None
    s = session or Session(engine)
    try:
        return s.exec(select(ProposalState).where(ProposalState.proposal_id == proposal_id)).first()
    finally:
        if own:
            s.close()
