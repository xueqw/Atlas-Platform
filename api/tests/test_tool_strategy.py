from app.tool_strategy import build_tool_policy


def _spec(name):
    return {"type": "function", "function": {"name": name, "description": "", "parameters": {}}}


def test_empty_specs_gives_empty_policy():
    p = build_tool_policy([], set(), {}, False)
    assert p == {"requires_tools": False, "tools": [], "write_tools": []}


def test_read_tool_tagged_read():
    p = build_tool_policy([_spec("search_repos")], set(), {}, True)
    assert p["requires_tools"] is True
    assert p["tools"] == [{"name": "search_repos", "connector": "github", "access": "read"}]
    assert p["write_tools"] == []


def test_static_connector_lookup_and_write():
    p = build_tool_policy([_spec("send_feishu_message")], {"send_feishu_message"},
                          {"send_feishu_message": "feishu"}, True)
    assert p["tools"] == [{"name": "send_feishu_message", "connector": "feishu", "access": "write"}]
    assert p["write_tools"] == ["send_feishu_message"]


def test_mcp_write_name_marks_write_and_defaults_github():
    p = build_tool_policy([_spec("create_issue")], {"create_issue"}, {}, True)
    assert p["tools"] == [{"name": "create_issue", "connector": "github", "access": "write"}]
    assert p["write_tools"] == ["create_issue"]


def test_mixed_read_and_write():
    specs = [_spec("search_repos"), _spec("create_issue")]
    p = build_tool_policy(specs, {"create_issue"}, {}, True)
    assert [t["access"] for t in p["tools"]] == ["read", "write"]
    assert p["write_tools"] == ["create_issue"]
