from app import strategy

CAT = [{"id": "s1", "name": "会议纪要", "description": "整理会议纪要", "trigger_phrases": "纪要"}]


def test_normalize_keeps_only_catalog_ids():
    data = {"goal": "g", "requires_knowledge": False, "requires_tools": False,
            "steps": [{"type": "respond", "title": "回答"}], "skills": ["s1", "ghost"]}
    out = strategy._normalize(data, has_kb=False, connectors=[], input_text="x", skill_catalog=CAT)
    assert out["skills"] == ["s1"]


def test_normalize_skills_default_empty():
    data = {"goal": "g", "requires_knowledge": False, "requires_tools": False,
            "steps": [{"type": "respond", "title": "回答"}]}
    out = strategy._normalize(data, has_kb=False, connectors=[], input_text="x", skill_catalog=CAT)
    assert out["skills"] == []


def test_fallback_has_empty_skills():
    fb = strategy.fallback_plan("hi", False, [])
    assert fb["skills"] == []
