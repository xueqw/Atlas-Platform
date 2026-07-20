"""Pure skill discovery and routing policy.

The module deliberately accepts plain dictionaries.  Storage, embeddings, and
HTTP handlers can adapt their own records at the boundary while this policy
remains deterministic and safe to test.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
import json
import math
from typing import Any
import uuid


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
    "trigger_phrases",
    "updated_at",
)

ALLOWED_SKILL_STATUSES = frozenset({"active", "draft", "candidate", "published", "deprecated", "disabled"})
ALLOWED_SORT_FIELDS = frozenset({"relevance", "name", "version", "updated_at"})


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


def validate_skill_metadata(skill: Mapping[str, Any]) -> dict[str, Any]:
    """Validate fields needed for governed discovery without touching content."""
    required = ("id", "name", "summary", "use_when", "do_not_use_when", "version", "status")
    missing = [field for field in required if field not in skill]
    if missing:
        raise ValueError(f"skill metadata missing required fields: {', '.join(missing)}")
    validate_category_path(skill.get("category_path", ()))
    if not isinstance(skill["use_when"], Sequence) or isinstance(skill["use_when"], (str, bytes)):
        raise ValueError("use_when must be an array")
    if not isinstance(skill["do_not_use_when"], Sequence) or isinstance(skill["do_not_use_when"], (str, bytes)):
        raise ValueError("do_not_use_when must be an array")
    if skill["status"] not in ALLOWED_SKILL_STATUSES:
        raise ValueError("invalid skill status")
    version_parts = str(skill["version"]).split(".")
    if len(version_parts) != 3 or any(not part.isdigit() for part in version_parts):
        raise ValueError("skill version must use numeric semantic versioning")
    for field in ("input_schema", "output_schema"):
        value = skill.get(field, {})
        if not isinstance(value, Mapping):
            raise ValueError(f"{field} must be an object")
        if value and value.get("type") not in {None, "object"}:
            raise ValueError(f"{field} root type must be object")
        if value and "properties" in value and not isinstance(value["properties"], Mapping):
            raise ValueError(f"{field}.properties must be an object")
        if value and "required" in value:
            required = value["required"]
            if not isinstance(required, Sequence) or isinstance(required, (str, bytes)):
                raise ValueError(f"{field}.required must be an array")
            properties = set(value.get("properties", {}))
            if not set(required).issubset(properties):
                raise ValueError(f"{field}.required must reference declared properties")
    permissions = skill.get("permissions", [])
    if not isinstance(permissions, Sequence) or isinstance(permissions, (str, bytes)):
        raise ValueError("permissions must be an array")
    if any(not str(permission).strip() or " " in str(permission) for permission in permissions):
        raise ValueError("permissions must contain non-empty identifiers without spaces")
    return safe_skill_metadata(skill)


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


def search_skill_metadata(
    skills: Iterable[Mapping[str, Any]],
    *,
    query: str = "",
    category_prefix: str | Sequence[str] | None = None,
    statuses: Iterable[str] | None = ("published",),
    required_permissions: Iterable[str] = (),
    permitted_ids: Iterable[str] | None = None,
    vector_scores: Mapping[str, float] | None = None,
    sort_by: str = "relevance",
    descending: bool = True,
    page: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    """Search/filter/sort metadata with stable pagination and no content leak."""
    if page < 1 or not 1 <= page_size <= 100:
        raise ValueError("page must be positive and page_size must be between 1 and 100")
    if sort_by not in ALLOWED_SORT_FIELDS:
        raise ValueError("unsupported sort field")
    prefix = validate_category_path(category_prefix) if category_prefix is not None else ()
    status_filter = set(statuses) if statuses is not None else None
    permission_filter = set(required_permissions)
    permitted = set(permitted_ids) if permitted_ids is not None else None
    vector_scores = vector_scores or {}
    query_terms = {term for term in query.lower().split() if term}
    matched: list[dict[str, Any]] = []
    for skill in skills:
        metadata = safe_skill_metadata(skill)
        skill_id = str(metadata.get("id", ""))
        path = tuple(metadata["category_path"])
        if permitted is not None and skill_id not in permitted:
            continue
        if prefix and path[: len(prefix)] != prefix:
            continue
        if status_filter is not None and metadata.get("status") not in status_filter:
            continue
        if not permission_filter.issubset(set(metadata.get("permissions", []))):
            continue
        haystack = " ".join(
            str(value)
            for value in (
                metadata.get("name", ""), metadata.get("summary", ""),
                *metadata.get("use_when", []), *metadata.get("trigger_phrases", []), *path,
            )
        ).lower()
        haystack_terms = set(haystack.split())
        lexical = len(query_terms & haystack_terms) / len(query_terms) if query_terms else 1.0
        if query_terms and query.lower() in haystack:
            lexical = max(lexical, 0.8)
        vector = max(0.0, min(1.0, float(vector_scores.get(skill_id, 0.0))))
        relevance = 0.55 * lexical + 0.45 * vector if query_terms else 1.0
        if query_terms and lexical == 0 and vector == 0:
            continue
        item = dict(metadata)
        item["relevance"] = round(relevance, 6)
        matched.append(item)

    def sort_value(item: Mapping[str, Any]) -> tuple[Any, str]:
        value: Any = item.get(sort_by, "")
        if sort_by == "relevance":
            value = float(value or 0)
        return value, str(item.get("id", ""))

    matched.sort(key=sort_value, reverse=descending)
    total = len(matched)
    start = (page - 1) * page_size
    items = matched[start : start + page_size]
    return {
        "items": items,
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": math.ceil(total / page_size) if total else 0,
        "sort_by": sort_by,
        "descending": descending,
    }


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


def _rerank_score(query: str, skill: Mapping[str, Any], recall_score: float) -> float:
    """Second-stage scenario reranker, deliberately independent of recall weights."""
    normalized = query.casefold()
    phrases = [
        str(value).strip().casefold()
        for value in (*skill.get("use_when", []), *skill.get("trigger_phrases", []))
        if str(value).strip()
    ]
    exact_scenario = max((1.0 if phrase in normalized else 0.0 for phrase in phrases), default=0.0)
    query_terms = set(normalized.split())
    scenario_terms = set(" ".join(phrases).split())
    scenario_overlap = len(query_terms & scenario_terms) / len(query_terms) if query_terms else 0.0
    # Recall remains the dominant signal, while scenario evidence can change order.
    return min(1.0, 0.70 * recall_score + 0.20 * scenario_overlap + 0.10 * exact_scenario)


def _select_tree_branch(
    skills: Sequence[Mapping[str, Any]], *, query: str,
    keyword_scores: Mapping[str, float], vector_scores: Mapping[str, float],
    keyword_weight: float, vector_weight: float,
) -> tuple[tuple[str, ...], dict[str, Any]]:
    """Choose a first-level tree branch before any Skill-level reranking."""
    branch_scores: dict[str, float] = {}
    for skill in skills:
        path = validate_category_path(skill.get("category_path", ("general",)))
        skill_id = str(skill["id"])
        lexical = _text_score(query, skill, keyword_scores.get(skill_id))
        vector = max(0.0, min(1.0, float(vector_scores.get(skill_id, 0.0))))
        score = keyword_weight * lexical + vector_weight * vector
        branch_scores[path[0]] = max(branch_scores.get(path[0], 0.0), score)
    ranked = sorted(branch_scores.items(), key=lambda item: (-item[1], item[0]))
    if not ranked:
        return (), {"decision": "no_branch", "scores": []}
    leader = ranked[0]
    runner_up = ranked[1] if len(ranked) > 1 else None
    if leader[1] <= 0:
        return (), {"decision": "no_branch", "scores": ranked, "reason": "branch_below_minimum_score"}
    if runner_up and leader[1] - runner_up[1] < 0.05:
        return (), {"decision": "ambiguous", "scores": ranked, "reason": "tree_branches_too_close"}
    return (leader[0],), {"decision": "selected", "scores": ranked, "selected": leader[0]}


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
    category_prefix: str | Sequence[str] | None = None,
    allowed_statuses: Iterable[str] = ("published",),
) -> dict[str, Any]:
    """Choose one runtime-eligible skill conservatively and emit a replayable audit."""
    if minimum_score < 0 or minimum_gap < 0:
        raise ValueError("routing thresholds must be non-negative")
    explicit_prefix = validate_category_path(category_prefix) if category_prefix is not None else ()
    supplied_skills = list(skills)
    routeable_statuses = set(allowed_statuses)
    keyword_scores = keyword_scores or {}
    vector_scores = vector_scores or {}
    if explicit_prefix:
        prefix = explicit_prefix
        tree_decision = {"decision": "explicit", "selected": "/".join(prefix), "scores": []}
    else:
        prefix, tree_decision = _select_tree_branch(
            supplied_skills, query=query, keyword_scores=keyword_scores, vector_scores=vector_scores,
            keyword_weight=keyword_weight, vector_weight=vector_weight,
        )
    routeable_skills = [
        skill for skill in supplied_skills
        if str(skill.get("status", "published")) in routeable_statuses
    ]
    branch_skills = [
        skill for skill in supplied_skills
        if prefix and tuple(validate_category_path(skill.get("category_path", ("general",))))[: len(prefix)] == prefix
        and str(skill.get("status", "published")) in routeable_statuses
    ]
    known_skills = {str(skill["id"]): skill for skill in routeable_skills}
    automatic_skills = {str(skill["id"]): skill for skill in branch_skills}
    permitted = set(permitted_ids)
    manual = [skill_id for skill_id in manual_ids if skill_id in known_skills]
    candidates: list[dict[str, Any]] = []

    for skill_id, skill in automatic_skills.items():
        if skill_id not in permitted or skill_id in manual:
            continue
        keyword_score = _text_score(query, skill, keyword_scores.get(skill_id))
        vector_score = max(0.0, min(1.0, float(vector_scores.get(skill_id, 0.0))))
        recall_score = keyword_weight * keyword_score + vector_weight * vector_score
        penalty, negative_matches = _negative_penalty(query, skill)
        rerank_score = max(0.0, _rerank_score(query, skill, recall_score) - penalty)
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
    if tree_decision.get("decision") in {"ambiguous", "no_branch"}:
        reasons.append(str(tree_decision.get("reason", "no_matching_tree_branch")))
        if tree_decision.get("decision") == "ambiguous":
            reasons.append("top_candidates_too_close")
    elif candidates:
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
    policy_blocked_manual = [skill_id for skill_id in manual_ids if skill_id not in known_skills]
    if policy_blocked_manual:
        reasons.append("manual_selection_filtered_by_status_or_tree")

    decision = "selected" if selected_ids else "no_selection"
    return {
        "decision_id": str(uuid.uuid4()),
        "schema_version": "v1",
        "category_path": list(prefix),
        "tree_stage": {
            "candidate_count_before": len(supplied_skills),
            "candidate_count_after": len(branch_skills),
        },
        "tree_decision": tree_decision,
        "query": query,
        "thresholds": {"minimum_score": minimum_score, "minimum_gap": minimum_gap},
        "recall": candidates,
        "rerank": candidates,
        "decision": decision,
        "reasons": reasons,
        "selected_ids": selected_ids,
        "selection_sources": sources,
    }


def load_selected_skill_content(
    skills: Iterable[Mapping[str, Any]], selected_ids: Iterable[str], *,
    permitted_ids: Iterable[str] | None = None,
    allowed_statuses: Iterable[str] = ("published",),
) -> list[dict[str, Any]]:
    """Progressive disclosure final stage: load body only for selected IDs."""
    by_id = {str(skill["id"]): skill for skill in skills}
    permitted = set(permitted_ids) if permitted_ids is not None else set(selected_ids)
    routeable_statuses = set(allowed_statuses)
    loaded: list[dict[str, Any]] = []
    for skill_id in selected_ids:
        skill = by_id.get(skill_id) if skill_id in permitted else None
        if skill is not None and str(skill.get("status", "published")) not in routeable_statuses:
            skill = None
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


def persist_router_decision(db: Any, *, scope: Any, decision: Mapping[str, Any]) -> Any:
    """Persist a replayable decision without coupling the pure router to an API layer."""
    from .governance_models import SkillRouterDecision

    record = SkillRouterDecision(
        decision_id=str(decision.get("decision_id") or uuid.uuid4()),
        workspace_id=scope.workspace_id,
        user_id=scope.user_id,
        agent_id=scope.agent_id,
        run_id=scope.run_id,
        query=str(decision.get("query", "")),
        inputs={
            "schema_version": decision.get("schema_version", "v1"),
            "category_path": list(decision.get("category_path", [])),
            "tree_stage": dict(decision.get("tree_stage", {})),
            "tree_decision": dict(decision.get("tree_decision", {})),
            "thresholds": dict(decision.get("thresholds", {})),
        },
        recall=list(decision.get("recall", [])),
        rerank=list(decision.get("rerank", [])),
        selected_ids=list(decision.get("selected_ids", [])),
        decision=str(decision.get("decision", "no_selection")),
        reasons=list(decision.get("reasons", [])),
        created_at=datetime.now(timezone.utc),
    )
    db.add(record)
    db.flush()
    return record


def decode_persisted_embedding(value: Any) -> list[float] | None:
    """Decode SQLite JSON or pgvector values into a stable numeric vector."""
    if value is None:
        return None
    if isinstance(value, str):
        value = json.loads(value)
    if hasattr(value, "tolist"):
        value = value.tolist()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return None
    return [float(item) for item in value]


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    if not left or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    norm = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    return max(0.0, min(1.0, dot / norm)) if norm else 0.0


def persisted_vector_scores(skills: Iterable[Any], query_vector: Sequence[float]) -> dict[str, float]:
    """Score persisted Skill embeddings; works for pgvector and SQLite test storage."""
    scores: dict[str, float] = {}
    for skill in skills:
        vector = decode_persisted_embedding(getattr(skill, "embedding", None))
        if vector is not None:
            scores[str(skill.id)] = cosine_similarity(query_vector, vector)
    return scores


def persisted_vector_scores_from_db(
    db: Any,
    skill_model: Any,
    *,
    workspace_id: str,
    query_vector: Sequence[float],
    permitted_ids: Iterable[str] | None = None,
    limit: int = 64,
    minimum_similarity: float = 0.0,
) -> dict[str, float]:
    """Use pgvector cosine distance in PostgreSQL and keep SQLite tests portable.

    The query is always tenant-scoped and can be narrowed to the policy-filtered
    Skill IDs. Callers may still use :func:`persisted_vector_scores` for an
    already-loaded SQLite catalog.
    """
    if not query_vector:
        return {}
    if limit < 1:
        raise ValueError("vector recall limit must be positive")
    bind = db.get_bind() if hasattr(db, "get_bind") else getattr(db, "bind", None)
    dialect_name = getattr(getattr(bind, "dialect", None), "name", "")
    if dialect_name == "sqlite":
        return {}
    if dialect_name != "postgresql":
        raise RuntimeError("persisted Skill vector retrieval requires PostgreSQL/pgvector or SQLite test fallback")
    from sqlalchemy import select

    conditions = [
        skill_model.workspace_id == workspace_id,
        skill_model.embedding.is_not(None),
    ]
    allowed = tuple(str(item) for item in permitted_ids) if permitted_ids is not None else ()
    if permitted_ids is not None:
        if not allowed:
            return {}
        conditions.append(skill_model.id.in_(allowed))
    distance = skill_model.embedding.cosine_distance(list(query_vector))
    similarity = (1 - distance).label("similarity")
    statement = (
        select(skill_model.id, similarity)
        .where(*conditions, distance <= 1.0 - minimum_similarity)
        .order_by(distance.asc(), skill_model.id.asc())
        .limit(limit)
    )
    rows = db.execute(statement).all()
    return {
        str(skill_id): max(0.0, min(1.0, float(score or 0.0)))
        for skill_id, score in rows
    }
