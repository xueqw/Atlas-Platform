from app.skills_engine import build_skill_instructions, resolve_skills

CAT = [
    {"id": "a", "name": "A", "content": "ca"},
    {"id": "b", "name": "B", "content": "cb"},
    {"id": "c", "name": "C", "content": "cc"},
]


def test_empty():
    assert resolve_skills(CAT, [], []) == []


def test_manual_only():
    out = resolve_skills(CAT, ["a"], [])
    assert out == [{"id": "a", "name": "A", "content": "ca", "source": "manual"}]


def test_auto_only():
    out = resolve_skills(CAT, [], ["b"])
    assert out == [{"id": "b", "name": "B", "content": "cb", "source": "auto"}]


def test_union_manual_first_overlap_is_manual():
    out = resolve_skills(CAT, ["a"], ["a", "b"])
    assert [(o["id"], o["source"]) for o in out] == [("a", "manual"), ("b", "auto")]


def test_dirty_ids_filtered():
    out = resolve_skills(CAT, ["zzz"], ["nope"])
    assert out == []


def test_order_manual_then_auto():
    out = resolve_skills(CAT, ["c"], ["a"])
    assert [o["id"] for o in out] == ["c", "a"]


def test_build_empty():
    assert build_skill_instructions([]) == ""


def test_build_includes_each_content():
    sel = [
        {"id": "a", "name": "会议纪要", "content": "按议题/结论/待办整理", "source": "manual"},
        {"id": "b", "name": "中英润色", "content": "保留术语润色", "source": "auto"},
    ]
    text = build_skill_instructions(sel)
    assert "技能指引" in text
    assert "会议纪要" in text and "按议题/结论/待办整理" in text
    assert "中英润色" in text and "保留术语润色" in text
