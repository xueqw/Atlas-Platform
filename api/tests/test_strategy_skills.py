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


def test_user_prompt_uses_metadata_without_content_gist():
    # 规划阶段只见安全元数据；正文必须等到选中后才会加载。
    cat = [{"id": "s1", "name": "周报助手", "description": "",
            "trigger_phrases": "", "content": "把零散记录整理成周报：①完成 ②进行中 ③下周计划"}]
    p = strategy._user_prompt("帮我汇报本周工作", has_kb=False, connectors=[], agent_prompt=None, skill_catalog=cat)
    assert "周报助手" in p
    assert "完成" not in p and "下周计划" not in p


def test_user_prompt_keeps_description_without_content():
    cat = [{"id": "s1", "name": "会议纪要", "description": "整理会议纪要",
            "trigger_phrases": "", "content": "①议题 ②结论 ③待办"}]
    p = strategy._user_prompt("x", has_kb=False, connectors=[], agent_prompt=None, skill_catalog=cat)
    assert "整理会议纪要" in p
    assert "待办" not in p
