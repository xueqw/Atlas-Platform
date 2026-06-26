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


def test_user_prompt_includes_name_and_content_gist():
    # 方案 B：描述留空时，name + content 要点仍要进路由提示词，让烂描述也能被命中
    cat = [{"id": "s1", "name": "周报助手", "description": "",
            "trigger_phrases": "", "content": "把零散记录整理成周报：①完成 ②进行中 ③下周计划"}]
    p = strategy._user_prompt("帮我汇报本周工作", has_kb=False, connectors=[], agent_prompt=None, skill_catalog=cat)
    assert "周报助手" in p
    assert "完成" in p and "下周计划" in p  # content 要点已注入，不再只靠 description


def test_user_prompt_keeps_description_when_present():
    cat = [{"id": "s1", "name": "会议纪要", "description": "整理会议纪要",
            "trigger_phrases": "", "content": "①议题 ②结论 ③待办"}]
    p = strategy._user_prompt("x", has_kb=False, connectors=[], agent_prompt=None, skill_catalog=cat)
    assert "整理会议纪要" in p and "待办" in p
