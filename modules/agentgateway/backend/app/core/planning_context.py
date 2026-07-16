"""Planning Context aggregation service (T1: planning-context-aggregation).

Aggregates multiple sources into the single ``planning_context`` object the
plan-first planner consumes (design doc §3 / §7.1). The planner SHALL read only
this unified object, never the scattered sources directly.

Design notes:
- Independent, pure-ish module so the read-only API and pytest can call it
  without a WebSocket (design D1).
- Every segment is ALWAYS present; missing data yields the segment's
  empty/default form rather than a dropped key.
- Per-builder try/except: any source failure degrades that one segment to its
  empty/default form (+ a warning log) and never aborts the whole aggregation
  (design D5 / spec "聚合降级不阻断").
- ``user_profile`` is structured master-data and MUST NOT be mixed with memory.
- ``internal_context.capabilities`` carries only structured refs (id/type/name),
  never raw markdown/prompt content.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from sqlmodel import Session, select

from app.core.database import engine
from app.models.db import CapabilityItem, MemoryItem, UserProfile, WorkspaceContext

_log = logging.getLogger(__name__)

# Runtime modes the planner may recommend (design doc §3.F internal_context).
RUNTIME_MODES = ["direct", "flow", "cron", "multi_agent"]

# How many memory rows to surface per layer (top-N, summaries only — never raw
# content/transcripts in the planning_context).
_MEMORY_LIMIT = 10
_CAPABILITY_LIMIT = 100

# Hard-coded fallbacks used when the source row is absent (e.g. DB not seeded).
_DEFAULT_USER_PROFILE: Dict[str, Any] = {
    "user_id": "default",
    "name": "默认用户",
    "role": "",
    "department": "",
    "industry": "",
    "permissions": [],
    "preferences": {"language": "zh-CN", "interaction_style": "proposal_first"},
}
_DEFAULT_WORKSPACE: Dict[str, Any] = {
    "workspace_id": "default",
    "workspace_name": "默认工作区",
    "project_context": "",
    "connected_systems": [],
    "default_entities": [],
    "default_capability_tags": [],
}


def _loads(raw: Any, default):
    """Tolerant JSON loader (mirrors memory_service._loads)."""
    if isinstance(raw, (list, dict)):
        return raw
    try:
        val = json.loads(raw) if raw else default
        return val if val is not None else default
    except (json.JSONDecodeError, TypeError):
        return default


def build_planning_context(
    *,
    conversation_id: Optional[str] = None,
    user_input: Optional[Dict[str, Any]] = None,
    user_id: Optional[str] = None,
    workspace_id: Optional[str] = None,
    session: Optional[Session] = None,
    db: Optional[Session] = None,
    on_step: Optional[Any] = None,
) -> Dict[str, Any]:
    """Build the unified ``planning_context`` dict (seven segments).

    All keyword-only. ``user_input`` is this turn's explicit input (dict).
    ``session``/``db`` are interchangeable optional DB sessions; when neither is
    given a short-lived one is opened. Never raises — each segment degrades
    independently.

    ``on_step`` (planner-granular-step-hooks) is an optional SYNC callback
    ``on_step(sub_step: str, phase: str, detail: dict)`` invoked before/after each
    real builder sub-step (phase ∈ "start"|"done"|"failed"), so the planner can
    surface a genuine step-by-step process. When omitted, behaviour and return
    are byte-for-byte unchanged.
    """
    s = session or db
    own_session = s is None
    if own_session:
        s = Session(engine)
    try:
        ctx = {
            "user_input": _safe("user_input", _build_user_input, user_input, on_step=on_step),
            "user_profile": _safe("user_profile", _build_user_profile, s, user_id, on_step=on_step),
            "memory_context": _safe("memory_context", _build_memory_context, s, on_step=on_step),
            "workspace_context": _safe("workspace_context", _build_workspace_context, s, workspace_id, on_step=on_step),
            "external_context": _safe("external_context", _build_external_context, s, on_step=on_step),
            "internal_context": _safe("internal_context", _build_internal_context, s, on_step=on_step),
            "policy_context": _safe("policy_context", _build_policy_context, on_step=on_step),
        }
        return ctx
    finally:
        if own_session:
            s.close()


def _step_detail(segment: str, result: Any) -> dict:
    """Extract a small, human-readable detail for a finished builder sub-step
    (planner-granular-step-hooks). Best-effort; never raises."""
    try:
        r = result if isinstance(result, dict) else {}
        if segment == "user_profile":
            return {"role": r.get("role") or "", "name": r.get("name") or ""}
        if segment == "workspace_context":
            return {"connected_systems": r.get("connected_systems") or []}
        if segment == "internal_context":
            return {"capability_count": len(r.get("capabilities") or []),
                    "expert_template_count": len(r.get("expert_templates") or [])}
        if segment == "memory_context":
            return {"provider": (r.get("memory_provider_status") or {}).get("provider", ""),
                    "preference_count": len(r.get("preference_memory") or [])}
        if segment == "external_context":
            return {"system_count": len(r.get("systems") or [])}
        if segment == "policy_context":
            return {"clarification_policy": r.get("clarification_policy") or ""}
        if segment == "user_input":
            return {"has_goal": bool(r.get("goal_text"))}
    except Exception:
        pass
    return {}


def _safe(segment: str, fn, *args, on_step: Optional[Any] = None):
    """Run a segment builder; on any failure log a warning and return that
    segment's empty/default fallback form. Keeps the overall aggregation total
    (design D5 / spec "聚合降级不阻断").

    When ``on_step`` is given, emits start/done/failed sub-step signals around the
    builder so each real sub-step is observable (planner-granular-step-hooks).
    Hook errors are swallowed — observability must never break aggregation."""
    if on_step is not None:
        try:
            on_step(segment, "start", {})
        except Exception:
            pass
    try:
        result = fn(*args)
    except Exception as exc:  # one bad source must not break the whole context
        _log.warning("planning_context %s failed (%s); degrading segment", segment, exc)
        if on_step is not None:
            try:
                on_step(segment, "failed", {"error": str(exc)[:200]})
            except Exception:
                pass
        return _SEGMENT_FALLBACKS.get(segment, lambda: {})()
    if on_step is not None:
        try:
            on_step(segment, "done", _step_detail(segment, result))
        except Exception:
            pass
    return result


# ── Segment builders ─────────────────────────────────────────────────────────


def _build_user_input(user_input: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Assemble user_input from this turn's request (goal_text / attachments /
    explicit_constraints / preferred_delivery)."""
    ui = user_input or {}
    return {
        "goal_text": str(ui.get("goal_text", "") or ""),
        "attachments": list(ui.get("attachments", []) or []),
        "explicit_constraints": list(ui.get("explicit_constraints", []) or []),
        "preferred_delivery": list(ui.get("preferred_delivery", []) or []),
    }


def _build_user_profile(s: Session, user_id: Optional[str]) -> Dict[str, Any]:
    """Read the user_profiles row (default when no id); fall back to the
    hard-coded default dict when the row is absent. Master-data only — never
    mixes in memory."""
    row = s.get(UserProfile, user_id or "default")
    if row is None:
        return dict(_DEFAULT_USER_PROFILE)
    return {
        "user_id": row.id,
        "name": row.name,
        "role": row.role,
        "department": row.department,
        "industry": row.industry,
        "permissions": _loads(row.permissions_json, []),
        "preferences": _loads(row.preferences_json, {}),
    }


def _build_memory_context(s: Session) -> Dict[str, Any]:
    """Lightweight structured placeholder: top-N preference rows + recent
    activity summaries (summaries only, no raw content) + provider status. No
    semantic retrieval at T1."""
    preference_memory: List[Dict[str, Any]] = []
    recent_activity_summary: List[str] = []
    try:
        pref_rows = s.exec(
            select(MemoryItem)
            .where(
                MemoryItem.status == "active",
                MemoryItem.memory_type.in_(["profile_preference", "profile_fact"]),
            )
            .order_by(MemoryItem.importance.desc())
            .limit(_MEMORY_LIMIT)
        ).all()
        for r in pref_rows:
            preference_memory.append({
                "key": r.summary or "",
                "value": r.summary or "",
                "confidence": r.confidence,
            })
        act_rows = s.exec(
            select(MemoryItem)
            .where(
                MemoryItem.status == "active",
                MemoryItem.memory_type.in_(["episodic", "summary"]),
            )
            .order_by(MemoryItem.updated_at.desc())
            .limit(_MEMORY_LIMIT)
        ).all()
        recent_activity_summary = [r.summary for r in act_rows if r.summary]
    except Exception as exc:
        # Memory store unavailable → empty layers, still return provider status.
        _log.warning("memory layers unavailable (%s); empty memory_context", exc)

    return {
        "preference_memory": preference_memory,
        "recent_activity_summary": recent_activity_summary,
        "memory_provider_status": _memory_provider_status(),
    }


def _memory_provider_status() -> Dict[str, Any]:
    """Reflect the configured memory provider's enabled/health state without
    forcing a live mem0 probe on the hot path."""
    try:
        from app.core.memory_provider import _select_provider_name, _env_bool

        name = _select_provider_name()
        return {
            "provider": name,
            "enabled": name != "none",
            "healthy": None,  # not probed here (avoid blocking the aggregator)
        }
    except Exception:
        return {"provider": "none", "enabled": False, "healthy": None}


def _build_workspace_context(s: Session, workspace_id: Optional[str]) -> Dict[str, Any]:
    """Read the workspace_contexts row (default when no id); fall back to the
    hard-coded default dict when absent."""
    row = s.get(WorkspaceContext, workspace_id or "default")
    if row is None:
        return dict(_DEFAULT_WORKSPACE)
    return {
        "workspace_id": row.id,
        "workspace_name": row.name,
        "project_context": row.project_context,
        "connected_systems": _loads(row.connected_systems_json, []),
        "default_entities": _loads(row.default_entities_json, []),
        "default_capability_tags": _loads(row.default_capability_tags_json, []),
    }


def _build_external_context(s: Session) -> Dict[str, Any]:
    """T1: no external binding source yet → empty systems list (key present).
    The entity/operation abstraction shape lives in the ExternalSystem schema."""
    return {"systems": []}


def _build_internal_context(s: Session) -> Dict[str, Any]:
    """Expose platform capabilities and cognitive assets, structured refs only
    (no raw markdown / prompt content).

    Type split (T3): ``type="expert_template"`` rows are cognitive assets — they
    go into ``expert_templates`` as ``{id, name, domain}`` (domain parsed from
    config), NOT into ``capabilities``. All other types stay in ``capabilities``
    as ``{id, type, name}``. This keeps cognitive assets and executable
    capabilities strictly layered. ``workflow_blueprints`` arrives in T10."""
    capabilities: List[Dict[str, Any]] = []
    expert_templates: List[Dict[str, Any]] = []
    rows = s.exec(select(CapabilityItem).limit(_CAPABILITY_LIMIT)).all()
    for r in rows:
        if r.type == "expert_template":
            domain = ""
            cfg = _loads(r.config, {})
            if isinstance(cfg, dict):
                domain = str(cfg.get("domain") or "")
            expert_templates.append({"id": r.id, "name": r.name, "domain": domain})
        else:
            capabilities.append({"id": r.id, "type": r.type, "name": r.name})
    return {
        "capabilities": capabilities,
        "runtime_modes": list(RUNTIME_MODES),
        "expert_templates": expert_templates,
        "workflow_blueprints": [],
    }


def _build_policy_context() -> Dict[str, Any]:
    """Governance defaults embodying the plan-first stance."""
    return {
        "clarification_policy": "minimal",
        "publish_policy": "draft_before_publish",
        "memory_policy_defaults": {
            "profile_context": True,
            "workspace_context": True,
            "proposal_history": True,
            "long_term_memory": False,
        },
        "runtime_restrictions": ["read_only_external_by_default"],
    }


# Per-segment empty/default fallbacks used by ``_safe`` when a builder raises
# before returning (keeps every segment key present even on hard failure).
_SEGMENT_FALLBACKS = {
    "user_input": lambda: {
        "goal_text": "", "attachments": [], "explicit_constraints": [],
        "preferred_delivery": [],
    },
    "user_profile": lambda: dict(_DEFAULT_USER_PROFILE),
    "memory_context": lambda: {
        "preference_memory": [], "recent_activity_summary": [],
        "memory_provider_status": {"provider": "none", "enabled": False, "healthy": None},
    },
    "workspace_context": lambda: dict(_DEFAULT_WORKSPACE),
    "external_context": lambda: {"systems": []},
    "internal_context": lambda: {
        "capabilities": [], "runtime_modes": list(RUNTIME_MODES),
        "expert_templates": [], "workflow_blueprints": [],
    },
    "policy_context": lambda: {
        "clarification_policy": "minimal", "publish_policy": "draft_before_publish",
        "memory_policy_defaults": {}, "runtime_restrictions": [],
    },
}
