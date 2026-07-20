import pytest

from app.skill_router import (
    build_category_tree,
    discover_skill_metadata,
    load_selected_skill_content,
    merge_selected_ids,
    persisted_vector_scores,
    persisted_vector_scores_from_db,
    route_skills,
    validate_skill_metadata,
)
from app.models import Skill
from sqlalchemy.dialects import postgresql


SKILLS = [
    {
        "id": "minutes",
        "name": "Meeting minutes",
        "category_path": ["work", "writing"],
        "summary": "Structure a meeting summary",
        "use_when": ["meeting notes"],
        "do_not_use_when": ["medical diagnosis"],
        "content": "SECRET: full minutes instructions",
        "version": "1.0.0",
        "status": "published",
    },
    {
        "id": "medical",
        "name": "Medical helper",
        "category_path": ["health"],
        "summary": "Health support",
        "do_not_use_when": ["meeting"],
        "content": "SECRET: medical instructions",
    },
]


def test_progressive_discovery_never_leaks_content():
    metadata = discover_skill_metadata(SKILLS)
    assert metadata[0]["id"] == "minutes"
    assert "content" not in metadata[0]
    assert "SECRET" not in str(metadata)
    assert load_selected_skill_content(SKILLS, ["minutes"])[0]["content"].startswith("SECRET")


def test_postgresql_skill_vector_recall_orders_and_limits_inside_database():
    captured = []

    class Database:
        def get_bind(self):
            return type("Bind", (), {"dialect": postgresql.dialect()})()

        def execute(self, statement):
            captured.append(str(statement.compile(dialect=postgresql.dialect())))
            return type("Rows", (), {"all": lambda self: []})()

    assert persisted_vector_scores_from_db(
        Database(), Skill, workspace_id="ws", query_vector=[1.0, 0.0],
        permitted_ids=["skill-1", "skill-2"], limit=8,
    ) == {}
    sql = captured[0]
    assert "<=>" in sql
    assert "ORDER BY" in sql and "LIMIT" in sql
    assert "skills.workspace_id" in sql and "skills.id IN" in sql


def test_skill_vector_recall_fallback_is_sqlite_only():
    class Database:
        def __init__(self, name):
            self.bind = type("Bind", (), {"dialect": type("Dialect", (), {"name": name})()})()

        def get_bind(self):
            return self.bind

    assert persisted_vector_scores_from_db(
        Database("sqlite"), Skill, workspace_id="ws", query_vector=[1.0]
    ) == {}
    with pytest.raises(RuntimeError, match="PostgreSQL/pgvector or SQLite"):
        persisted_vector_scores_from_db(
            Database("mysql"), Skill, workspace_id="ws", query_vector=[1.0]
        )


def test_negative_scenario_is_not_selected():
    audit = route_skills(
        SKILLS,
        query="Please prepare meeting notes",
        permitted_ids={"minutes", "medical"},
        keyword_scores={"minutes": 0.9, "medical": 0.1},
        vector_scores={"minutes": 0.9, "medical": 0.1},
    )
    assert audit["selected_ids"] == ["minutes"]
    assert "medical" not in audit["selected_ids"]


def test_threshold_and_close_candidates_produce_no_selection():
    low = route_skills(
        SKILLS,
        query="unrelated request",
        permitted_ids={"minutes"},
        keyword_scores={"minutes": 0.1},
        vector_scores={"minutes": 0.1},
    )
    assert low["decision"] == "no_selection"
    assert "top_candidate_below_minimum_score" in low["reasons"]

    close = route_skills(
        SKILLS,
        query="request",
        permitted_ids={"minutes", "medical"},
        keyword_scores={"minutes": 0.9, "medical": 0.88},
        vector_scores={"minutes": 0.9, "medical": 0.88},
    )
    assert close["decision"] == "no_selection"
    assert "top_candidates_too_close" in close["reasons"]


def test_manual_and_automatic_union_is_permission_filtered():
    selected, sources = merge_selected_ids(
        manual_ids=["medical", "minutes"],
        automatic_ids=["minutes"],
        permitted_ids={"minutes"},
    )
    assert selected == ["minutes"]
    assert sources == {"minutes": "manual"}

    audit = route_skills(
        SKILLS,
        query="meeting notes",
        permitted_ids={"minutes"},
        manual_ids=["medical", "minutes"],
        keyword_scores={"minutes": 0.9},
        vector_scores={"minutes": 0.9},
    )
    assert audit["selected_ids"] == ["minutes"]
    assert audit["selection_sources"] == {"minutes": "manual"}
    assert "manual_selection_filtered_by_permissions" in audit["reasons"]


def test_category_tree_has_three_level_limit():
    tree = build_category_tree(SKILLS)
    assert "work" in tree["children"]
    with pytest.raises(ValueError, match="between one and three"):
        discover_skill_metadata(
            [{"id": "too-deep", "category_path": ["a", "b", "c", "d"]}]
        )


def test_tree_first_routing_and_distinct_rerank_scores():
    audit = route_skills(
        SKILLS,
        query="meeting notes",
        permitted_ids={"minutes", "medical"},
        keyword_scores={"minutes": 0.9, "medical": 0.2},
        vector_scores={"minutes": 0.8, "medical": 0.7},
    )
    assert audit["tree_decision"]["selected"] == "work"
    assert [candidate["id"] for candidate in audit["recall"]] == ["minutes"]
    assert audit["recall"][0]["recall_score"] != audit["rerank"][0]["rerank_score"]


def test_full_metadata_validation_and_persisted_vector_scoring():
    valid = {**SKILLS[0], "input_schema": {"type": "object", "properties": {"notes": {"type": "string"}}, "required": ["notes"]}, "permissions": ["calendar.read"]}
    assert validate_skill_metadata(valid)["id"] == "minutes"
    with pytest.raises(ValueError, match="semantic versioning"):
        validate_skill_metadata({**valid, "version": "latest"})
    with pytest.raises(ValueError, match="declared properties"):
        validate_skill_metadata({**valid, "input_schema": {"type": "object", "properties": {}, "required": ["missing"]}})

    class Stored:
        id = "minutes"
        embedding = "[1.0, 0.0]"

    assert persisted_vector_scores([Stored()], [1.0, 0.0]) == {"minutes": 1.0}
