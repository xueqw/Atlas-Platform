"""Pure skill discovery and routing policy.

The module deliberately accepts plain dictionaries.  Storage, embeddings, and
HTTP handlers can adapt their own records at the boundary while this policy
remains deterministic and safe to test.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any


SAFE_METADATA_FIELDS = (
    "id",
    "name",
    "category_path",
    "summary",
    "use_when",
    "do_not_use_when",
    "examples",
    "input_schema",
    "output_schema",
    "requirements",
    "permissions",
    "version",
    "status",
)


def validate_category_path(category_path: str | Sequence[str]) -> tuple[str, ...]:
    """Normalize and validate a skill tree path with at most three levels."""
    if isinstance(category_path, str):
        parts = tuple(part.strip() for part in category_path.split("/") if part.strip())
    else:
        parts = tuple(str(part).strip() for part in category_path)
    if not 1 <= len(parts) <= 3 or any(not part for part in parts):
        raise ValueError("category_path must contain between one and three non-empty levels")
    return parts


def safe_skill_metadata(skill: Mapping[str, Any]) -> dict[str, Any]:
    """Return discovery-safe metadata and never expose the skill body."""
    category_path = validate_category_path(skill.get("category_path", ("general",)))
    metadata = {field: skill[field] for field in SAFE_METADATA_FIELDS if field in skill}
    metadata["category_path"] = list(category_path)
    return metadata


def discover_skill_metadata(
    skills: Iterable[Mapping[str, Any]],
    *,
    category_prefix: str | Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """Progressive disclosure stage: expose metadata for a tree branch only."""
    prefix = validate_category_path(category_prefix) if category_prefix is not None else ()
    discovered: list[dict[str, Any]] = []
    for skill in skills:
        metadata = safe_skill_metadata(skill)
        path = tuple(metadata["category_path"])
        if path[: len(prefix)] == prefix:
            discovered.append(metadata)
    return discovered


def build_category_tree(skills: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Build a metadata-only tree suitable for the first discovery step."""
    root: dict[str, Any] = {"children": {}}
    for skill in skills:
        node = root
        for part in validate_category_path(skill.get("category_path", ("general",))):
            node = node["children"].setdefault(part, {"children": {}})
        node.setdefault("skill_ids", []).append(skill["id"])
    return root


def merge_selected_ids(
    manual_ids: Iterable[str],
    automatic_ids: Iterable[str],
    permitted_ids: Iterable[str],
) -> tuple[list[str], dict[str, str]]:
    """Merge manual and automatic choices, then enforce the permission boundary."""
    permitted = set(permitted_ids)
    selected: list[str] = []
    sources: dict[str, str] = {}
    for skill_id in manual_ids:
        if skill_id in permitted and skill_id not in sources:
            selected.append(skill_id)
            sources[skill_id] = "manual"
    for skill_id in automatic_ids:
        if skill_id in permitted and skill_id not in sources:
            selected.append(skill_id)
            sources[skill_id] = "automatic"
    return selected, sources


def _text_score(query: str, skill: Mapping[str, Any], supplied_score: float | None) -> float:
    if supplied_score is not None:
        return max(0.0, min(1.0, float(supplied_score)))
    query_terms = {term for term in query.lower().split() if term}
    keyword_text = " ".join(
        str(value)
        for value in (
            skill.get("name", ""),
            skill.get("summary", ""),
            *skill.get("use_when", []),
            *skill.get("trigger_phrases", []),
        )
    ).lower()
    keywords = {term for term in keyword_text.split() if term}
    return len(query_terms & keywords) / len(query_terms) if query_terms else 0.0


def _negative_penalty(query: str, skill: Mapping[str, Any]) -> tuple[float, list[str]]:
    query_lower = query.lower()
    matches = [
        str(negative)
        for negative in skill.get("do_not_use_when", [])
        if str(negative).lower() in query_lower
    ]
    return (0.45 * len(matches), matches)


def route_skills(
    skills: Iterable[Mapping[str, Any]],
    *,
    query: str,
    permitted_ids: Iterable[str],
    manual_ids: Iterable[str] = (),
    keyword_scores: Mapping[str, float] | None = None,
    vector_scores: Mapping[str, float] | None = None,
    minimum_score: float = 0.55,
    minimum_gap: float = 0.10,
    keyword_weight: float = 0.45,
    vector_weight: float = 0.55,
) -> dict[str, Any]:
    """Choose one automatic skill conservatively and emit a replayable audit."""
    if minimum_score < 0 or minimum_gap < 0:
        raise ValueError("routing thresholds must be non-negative")
    known_skills = {str(skill["id"]): skill for skill in skills}
    permitted = set(permitted_ids)
    manual = [skill_id for skill_id in manual_ids if skill_id in known_skills]
    keyword_scores = keyword_scores or {}
    vector_scores = vector_scores or {}
    candidates: list[dict[str, Any]] = []

    for skill_id, skill in known_skills.items():
        if skill_id not in permitted or skill_id in manual:
            continue
        keyword_score = _text_score(query, skill, keyword_scores.get(skill_id))
        vector_score = max(0.0, min(1.0, float(vector_scores.get(skill_id, 0.0))))
        recall_score = keyword_weight * keyword_score + vector_weight * vector_score
        penalty, negative_matches = _negative_penalty(query, skill)
        rerank_score = max(0.0, recall_score - penalty)
        candidates.append(
            {
                "id": skill_id,
                "recall_score": round(recall_score, 6),
                "rerank_score": round(rerank_score, 6),
                "negative_matches": negative_matches,
            }
        )

    candidates.sort(key=lambda candidate: (-candidate["rerank_score"], candidate["id"]))
    automatic_ids: list[str] = []
    reasons: list[str] = []
    if candidates:
        leader = candidates[0]
        runner_up = candidates[1] if len(candidates) > 1 else None
        if leader["negative_matches"]:
            reasons.append("top_candidate_matches_negative_scenario")
        elif leader["rerank_score"] < minimum_score:
            reasons.append("top_candidate_below_minimum_score")
        elif runner_up and leader["rerank_score"] - runner_up["rerank_score"] < minimum_gap:
            reasons.append("top_candidates_too_close")
        else:
            automatic_ids.append(leader["id"])
            reasons.append("automatic_candidate_selected")
    else:
        reasons.append("no_permitted_automatic_candidate")

    selected_ids, sources = merge_selected_ids(manual, automatic_ids, permitted)
    blocked_manual = [skill_id for skill_id in manual_ids if skill_id not in permitted]
    if blocked_manual:
        reasons.append("manual_selection_filtered_by_permissions")

    decision = "selected" if selected_ids else "no_selection"
    return {
        "recall": candidates,
        "rerank": candidates,
        "decision": decision,
        "reasons": reasons,
        "selected_ids": selected_ids,
        "selection_sources": sources,
    }


def load_selected_skill_content(
    skills: Iterable[Mapping[str, Any]], selected_ids: Iterable[str]
) -> list[dict[str, Any]]:
    """Progressive disclosure final stage: load body only for selected IDs."""
    by_id = {str(skill["id"]): skill for skill in skills}
    loaded: list[dict[str, Any]] = []
    for skill_id in selected_ids:
        skill = by_id.get(skill_id)
        if skill is None:
            continue
        loaded.append(
            {
                "id": skill_id,
                "name": skill.get("name", skill_id),
                "content": skill.get("content", ""),
            }
        )
    return loaded
