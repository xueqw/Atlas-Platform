from app.deploy_policy import build_publish_checklist, is_high_risk_connector


def _checklist_of(auth_client, agent_id):
    r = auth_client.get(f"/api/agents/{agent_id}/publish-checklist")
    assert r.status_code == 200, r.text
    return r.json()


def _item(checklist, key):
    return next(i for i in checklist["items"] if i["key"] == key)


def test_fresh_code_agent_blocked_on_everything(auth_client):
    draft_id = auth_client.post("/api/apps/generate", json={"message": "客服助手", "project_name": ""}).json()["draft"]["id"]

    checklist = _checklist_of(auth_client, draft_id)
    assert _item(checklist, "has_version")["ok"] is True  # generate 已经建了 v1
    assert _item(checklist, "passed_test")["ok"] is False
    assert _item(checklist, "has_eval")["ok"] is False
    assert _item(checklist, "permissions_configured")["ok"] is False
    assert checklist["can_publish"] is False


def test_run_test_flips_passed_test_item(auth_client):
    draft_id = auth_client.post("/api/apps/generate", json={"message": "流程助手", "project_name": ""}).json()["draft"]["id"]

    r = auth_client.post(f"/api/apps/drafts/{draft_id}/run", json={"input_text": "hi"})
    assert r.status_code == 200, r.text

    checklist = _checklist_of(auth_client, draft_id)
    assert _item(checklist, "passed_test")["ok"] is True
    assert _item(checklist, "has_eval")["ok"] is False  # 还没跑评测


def test_evaluate_flips_has_eval_item(auth_client):
    draft_id = auth_client.post("/api/apps/generate", json={"message": "招聘助手", "project_name": ""}).json()["draft"]["id"]

    r = auth_client.post(f"/api/apps/drafts/{draft_id}/evaluate", json={
        "cases": [{"name": "基础", "input": "hi", "expected": ""}],
    })
    assert r.status_code == 200, r.text

    checklist = _checklist_of(auth_client, draft_id)
    assert _item(checklist, "has_eval")["ok"] is True


def test_deploy_config_save_flips_permissions_item(auth_client):
    agent = auth_client.post("/api/agents", json={"name": "E"}).json()

    checklist = _checklist_of(auth_client, agent["id"])
    assert _item(checklist, "permissions_configured")["ok"] is False

    auth_client.put(f"/api/agents/{agent['id']}/deploy-config", json={
        "visibility": "workspace", "shared_user_ids": [], "allowed_knowledge_base_ids": [],
        "allowed_skill_ids": [], "allowed_connectors": [], "write_confirm": True,
        "api_access": False, "call_log_enabled": True,
    })

    checklist = _checklist_of(auth_client, agent["id"])
    assert _item(checklist, "permissions_configured")["ok"] is True


def test_publish_blocked_with_missing_items_listed(auth_client):
    draft_id = auth_client.post("/api/apps/generate", json={"message": "文档助手", "project_name": ""}).json()["draft"]["id"]

    r = auth_client.post(f"/api/agents/{draft_id}/publish")
    assert r.status_code == 403, r.text
    detail = r.json()["detail"]
    assert "已通过基础测试" in detail
    assert "已有评测结果" in detail
    assert "已配置落地权限" in detail


def test_publish_succeeds_once_all_blocking_items_pass(auth_client):
    draft_id = auth_client.post("/api/apps/generate", json={"message": "数据分析助手", "project_name": ""}).json()["draft"]["id"]

    auth_client.post(f"/api/apps/drafts/{draft_id}/run", json={"input_text": "hi"})
    auth_client.post(f"/api/apps/drafts/{draft_id}/evaluate", json={
        "cases": [{"name": "基础", "input": "hi", "expected": ""}],
    })
    auth_client.put(f"/api/agents/{draft_id}/deploy-config", json={
        "visibility": "workspace", "shared_user_ids": [], "allowed_knowledge_base_ids": [],
        "allowed_skill_ids": [], "allowed_connectors": [], "write_confirm": True,
        "api_access": False, "call_log_enabled": True,
    })

    checklist = _checklist_of(auth_client, draft_id)
    assert checklist["can_publish"] is True

    r = auth_client.post(f"/api/agents/{draft_id}/publish")
    assert r.status_code == 200, r.text


def test_high_risk_connector_warns_but_does_not_block(auth_client):
    draft_id = auth_client.post("/api/apps/generate", json={"message": "客服助手", "project_name": ""}).json()["draft"]["id"]

    auth_client.post(f"/api/apps/drafts/{draft_id}/run", json={"input_text": "hi"})
    auth_client.post(f"/api/apps/drafts/{draft_id}/evaluate", json={
        "cases": [{"name": "基础", "input": "hi", "expected": ""}],
    })
    auth_client.put(f"/api/agents/{draft_id}/deploy-config", json={
        "visibility": "workspace", "shared_user_ids": [], "allowed_knowledge_base_ids": [],
        "allowed_skill_ids": [], "allowed_connectors": ["feishu"], "write_confirm": True,
        "api_access": False, "call_log_enabled": True,
    })

    checklist = _checklist_of(auth_client, draft_id)
    assert _item(checklist, "no_high_risk_connector")["ok"] is False
    assert checklist["can_publish"] is True  # warning 不阻断

    r = auth_client.post(f"/api/agents/{draft_id}/publish")
    assert r.status_code == 200, r.text


def test_prompt_agent_does_not_need_test_or_eval_to_publish(auth_client):
    """prompt 型没有代码沙箱测试/评测机制，这两项检查对它视为始终满足。"""
    agent = auth_client.post("/api/agents", json={"name": "F"}).json()

    checklist = _checklist_of(auth_client, agent["id"])
    assert _item(checklist, "passed_test")["ok"] is True
    assert _item(checklist, "has_eval")["ok"] is True
    assert _item(checklist, "permissions_configured")["ok"] is False  # 权限这一项仍然要配置

    auth_client.put(f"/api/agents/{agent['id']}/deploy-config", json={
        "visibility": "workspace", "shared_user_ids": [], "allowed_knowledge_base_ids": [],
        "allowed_skill_ids": [], "allowed_connectors": [], "write_confirm": True,
        "api_access": False, "call_log_enabled": True,
    })

    r = auth_client.post(f"/api/agents/{agent['id']}/publish")
    assert r.status_code == 200, r.text


def test_publish_checklist_agent_not_found(auth_client):
    r = auth_client.get("/api/agents/nonexistent-id/publish-checklist")
    assert r.status_code == 404


# === deploy_policy 纯函数单测 ===

def test_is_high_risk_connector_static_writes():
    assert is_high_risk_connector("feishu") is True
    assert is_high_risk_connector("github") is True


def test_is_high_risk_connector_readonly_mcp():
    assert is_high_risk_connector("amap") is False
    assert is_high_risk_connector("baidu_maps") is False


def test_is_high_risk_connector_unknown_provider():
    assert is_high_risk_connector("some-unknown-provider") is False


def test_build_publish_checklist_prompt_bypasses_test_and_eval():
    result = build_publish_checklist(
        kind="prompt", has_current_version=True, has_passed_test=False,
        last_eval_ok=None, deploy_config_configured=True, allowed_connectors=[],
    )
    assert result["can_publish"] is True


def test_build_publish_checklist_code_requires_test_and_eval():
    result = build_publish_checklist(
        kind="code", has_current_version=True, has_passed_test=False,
        last_eval_ok=None, deploy_config_configured=True, allowed_connectors=[],
    )
    assert result["can_publish"] is False


def test_build_publish_checklist_warning_never_blocks():
    result = build_publish_checklist(
        kind="prompt", has_current_version=True, has_passed_test=True,
        last_eval_ok=True, deploy_config_configured=True, allowed_connectors=["feishu"],
    )
    assert result["can_publish"] is True
    warning_items = [i for i in result["items"] if i["level"] == "warning"]
    assert any(not i["ok"] for i in warning_items)  # 高风险连接器确实标记未通过
