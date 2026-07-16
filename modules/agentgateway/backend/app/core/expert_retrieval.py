"""Expert Template retrieval service (T3: expert-template-ingestion-and-retrieval).

Planner-callable retrieval over ``CapabilityItem(type="expert_template")`` rows.
Given a goal text and/or planning_context, returns the top-k expert candidates
as structured refs — id/name/domain/score, NEVER the persona / raw markdown
(cognitive assets are referenced, not inlined; design D3 / spec "候选不含原文").

Scoring is a transparent heuristic (mirrors the T2 capability matcher), not a
learned ranker (that is T4/later):
  - domain_hint or the workspace's default_capability_tags hitting the template's
    domain / tags → strong weight
  - the template's name / deliverables / tags overlapping the goal text → weight
Top-k by score desc; no template / no match → [].
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from sqlmodel import Session, select

from app.core.database import engine
from app.models.db import CapabilityItem

_log = logging.getLogger(__name__)

_DOMAIN_WEIGHT = 5      # explicit domain hint hits the template's domain
_TAG_WEIGHT = 2         # workspace default_capability_tag hits a template tag/domain
_TEXT_WEIGHT = 1        # name / deliverable / tag token overlaps the goal text


def _loads(raw: Any, default):
    if isinstance(raw, (list, dict)):
        return raw
    try:
        v = json.loads(raw) if raw else default
        return v if v is not None else default
    except (json.JSONDecodeError, TypeError):
        return default


def retrieve_expert_candidates(
    *,
    goal_text: str = "",
    planning_context: Optional[Dict[str, Any]] = None,
    domain_hint: str = "",
    limit: int = 5,
    session: Optional[Session] = None,
) -> List[Dict[str, Any]]:
    """Return top-k expert_template candidates as ``{id, name, domain, score}``.

    Never raises on bad config (tolerant parse). Returns [] when there are no
    expert_template rows or nothing scores above zero AND there is no signal to
    rank by (so an empty/irrelevant query yields [], not noise)."""
    own = session is None
    s = session or Session(engine)
    try:
        rows = s.exec(
            select(CapabilityItem).where(CapabilityItem.type == "expert_template")
        ).all()
    except Exception as exc:
        _log.warning("expert retrieval query failed (%s); returning []", exc)
        if own:
            s.close()
        return []

    try:
        ctx = planning_context if isinstance(planning_context, dict) else {}
        # Workspace default capability tags act as a soft domain/tag signal.
        ws_tags = [
            str(t).lower()
            for t in ((ctx.get("workspace_context") or {}).get("default_capability_tags") or [])
        ]
        goal = (goal_text or str((ctx.get("user_input") or {}).get("goal_text") or "")).lower()
        dhint = (domain_hint or "").lower()

        scored: List[tuple] = []
        for r in rows:
            cfg = _loads(r.config, {})
            cfg = cfg if isinstance(cfg, dict) else {}
            domain = str(cfg.get("domain") or "")
            domain_l = domain.lower()
            tags = [str(t).lower() for t in (_loads(r.tags, []) or [])]
            deliverables = [str(d).lower() for d in (cfg.get("deliverables") or [])]
            name_l = (r.name or "").lower()
            # T4: surface the template's recommendations so the caller can feed
            # capability match + runtime mode without a second DB read. Still
            # structured-only (no persona / raw markdown).
            rec_tags = [str(t) for t in (cfg.get("recommended_capability_tags") or [])]
            rec_modes = [str(m) for m in (cfg.get("recommended_modes") or [])]

            score = 0
            if dhint and (dhint == domain_l or dhint in domain_l):
                score += _DOMAIN_WEIGHT
            for wt in ws_tags:
                if wt and (wt == domain_l or wt in tags):
                    score += _TAG_WEIGHT
            if goal:
                tokens = {domain_l, name_l, *tags, *deliverables}
                for tok in tokens:
                    if tok and tok in goal:
                        score += _TEXT_WEIGHT
            scored.append((score, {
                "id": r.id, "name": r.name, "domain": domain, "score": score,
                "recommended_capability_tags": rec_tags,
                "recommended_modes": rec_modes,
            }))

        # Only surface candidates with a positive signal. When nothing matches,
        # return [] (spec: 无匹配返回空列表) rather than arbitrary templates.
        hits = [ref for sc, ref in sorted(scored, key=lambda t: t[0], reverse=True) if sc > 0]
        return hits[:limit]
    except Exception as exc:
        _log.warning("expert retrieval scoring failed (%s); returning []", exc)
        return []
    finally:
        if own:
            s.close()
