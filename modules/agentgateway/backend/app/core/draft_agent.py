"""Proposal → Draft Agent conversion (T5: proposal-draft-agent-lifecycle).

Derives a pre-publish ``DraftAgent`` from a CONFIRMED proposal (design §6.10 /
D5). Pure staging: it never creates an Agent or DAGGraph — compilation into
those runnable artifacts is a later stage (T6). Idempotent per proposal.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from sqlmodel import Session, select

from app.core.database import engine
from app.models.db import DraftAgent, Proposal, _utcnow

_log = logging.getLogger(__name__)


def _loads(raw, default):
    try:
        v = json.loads(raw) if raw else default
        return v if v is not None else default
    except (json.JSONDecodeError, TypeError):
        return default


def _derive_config(proposal_payload: dict) -> dict:
    """Derive the draft agent's structured config from the proposal payload.

    Prefers an explicit ``agent_spec`` (identity/runtime/memory/...) when the
    planner emitted one; otherwise assembles a minimal spec from the flat
    Proposal Schema fields. Capability refs and raw content are NOT inlined here
    (capability_refs_json carries the structured refs separately)."""
    spec = proposal_payload.get("agent_spec")
    if isinstance(spec, dict) and spec:
        return spec
    return {
        "identity": {
            "title": proposal_payload.get("title", ""),
            "goal": proposal_payload.get("goal", ""),
        },
        "runtime": {"runtime_mode": proposal_payload.get("runtime_mode", "direct")},
        "deliverables": proposal_payload.get("deliverables", []),
    }


def convert_proposal_to_draft(proposal_id: int, *, session: Optional[Session] = None) -> Optional[DraftAgent]:
    """Convert a confirmed proposal into a draft agent (idempotent).

    Returns the DraftAgent on success, or None when the proposal is missing or
    not in ``confirmed`` status (confirm-before-draft, D5). Re-running for the
    same proposal updates the existing draft rather than duplicating it."""
    own = session is None
    s = session or Session(engine)
    try:
        proposal = s.get(Proposal, proposal_id)
        if proposal is None:
            return None
        if proposal.status != "confirmed":
            _log.info("convert refused: proposal %s is %s (need confirmed)", proposal_id, proposal.status)
            return None

        payload = _loads(proposal.proposal_json, {})
        payload = payload if isinstance(payload, dict) else {}
        config = _derive_config(payload)
        capability_refs = payload.get("recommended_capabilities") or []
        runtime_mode = str(payload.get("runtime_mode") or proposal.selected_runtime_mode or "direct")
        memory_policy = {}
        if isinstance(config.get("memory"), dict):
            memory_policy = config["memory"]
        name = str(payload.get("title") or proposal.inferred_goal or "draft-agent")[:200]

        row = s.exec(select(DraftAgent).where(DraftAgent.proposal_id == proposal_id)).first()
        if row is None:
            row = DraftAgent(proposal_id=proposal_id, status="draft")
            s.add(row)
        row.name = name
        row.config_json = json.dumps(config, ensure_ascii=False)
        row.capability_refs_json = json.dumps(capability_refs, ensure_ascii=False)
        row.runtime_mode = runtime_mode
        row.memory_policy_json = json.dumps(memory_policy, ensure_ascii=False)
        row.updated_at = _utcnow()
        s.add(row)
        s.commit()
        s.refresh(row)
        return row
    finally:
        if own:
            s.close()


def get_draft_agent_by_proposal(proposal_id: int, *, session: Optional[Session] = None) -> Optional[DraftAgent]:
    own = session is None
    s = session or Session(engine)
    try:
        return s.exec(select(DraftAgent).where(DraftAgent.proposal_id == proposal_id)).first()
    finally:
        if own:
            s.close()


# Legal draft-agent status advances (T6). Only forward staging moves; publish is
# handled separately by the real apply flow.
_DRAFT_TRANSITIONS = {
    "draft": {"tested"},
    "tested": {"published"},
    "published": set(),
}


def transition_draft_status(draft_agent_id: int, target: str, *, session: Optional[Session] = None) -> bool:
    """Advance a draft agent's status if the transition is legal (T6 D6).
    Idempotent on the target (re-advancing to the same status is a no-op success).
    Returns False on illegal transition or missing row."""
    own = session is None
    s = session or Session(engine)
    try:
        row = s.get(DraftAgent, draft_agent_id)
        if row is None:
            return False
        if row.status == target:
            return True
        if target not in _DRAFT_TRANSITIONS.get(row.status, set()):
            _log.info("rejected draft transition %s -> %s (id=%s)", row.status, target, draft_agent_id)
            return False
        row.status = target
        row.updated_at = _utcnow()
        s.add(row)
        s.commit()
        return True
    finally:
        if own:
            s.close()


def get_draft_agent(draft_agent_id: int, *, session: Optional[Session] = None) -> Optional[DraftAgent]:
    own = session is None
    s = session or Session(engine)
    try:
        return s.get(DraftAgent, draft_agent_id)
    finally:
        if own:
            s.close()
