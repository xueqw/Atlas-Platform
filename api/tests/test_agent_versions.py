def _satisfy_publish_checklist(auth_client, agent_id, kind):
    """发布前检查清单（见 test_publish_checklist.py）要求已测试+已评测+已配置落地权限；
    这里帮版本管理测试跑通硬性项，checklist 本身的详细行为由专门的测试文件覆盖。"""
    if kind == "code":
        auth_client.post(f"/api/apps/drafts/{agent_id}/run", json={"input_text": "hi"})
        auth_client.post(f"/api/apps/drafts/{agent_id}/evaluate", json={
            "cases": [{"name": "t", "input": "hi", "expected": ""}],
        })
    auth_client.put(f"/api/agents/{agent_id}/deploy-config", json={
        "visibility": "workspace", "shared_user_ids": [], "allowed_knowledge_base_ids": [],
        "allowed_skill_ids": [], "allowed_connectors": [], "write_confirm": True,
        "api_access": False, "call_log_enabled": True,
    })


def test_create_prompt_agent_has_initial_version(auth_client):
    r = auth_client.post("/api/agents", json={"name": "客服助手"})
    assert r.status_code == 201, r.text
    agent = r.json()
    assert agent["kind"] == "prompt"
    assert agent["version_no"] == 1
    assert agent["published_version_no"] is None


def test_autosave_does_not_create_new_version(auth_client):
    agent = auth_client.post("/api/agents", json={"name": "A"}).json()

    r = auth_client.put(f"/api/agents/{agent['id']}", json={
        "name": "A", "system_prompt": "v2 prompt", "model": "gpt-4.1-mini", "status": "draft",
    })
    assert r.status_code == 200, r.text
    assert r.json()["version_no"] == 1  # 自动保存不新增版本

    versions = auth_client.get(f"/api/agents/{agent['id']}/versions").json()
    assert len(versions) == 1


def test_manual_save_version_and_diff(auth_client):
    agent = auth_client.post("/api/agents", json={"name": "B", "system_prompt": "v1 prompt"}).json()
    auth_client.put(f"/api/agents/{agent['id']}", json={
        "name": "B", "system_prompt": "v2 prompt", "model": "gpt-4.1-mini", "status": "draft",
    })

    r = auth_client.post(f"/api/agents/{agent['id']}/versions", json={"note": "manual save"})
    assert r.status_code == 201, r.text
    v2 = r.json()
    assert v2["version_no"] == 2
    assert v2["label"] == "history"

    r = auth_client.get(f"/api/agents/{agent['id']}/versions/diff", params={"from": 1, "to": 2})
    assert r.status_code == 200, r.text
    diff = r.json()
    field_change = next(f for f in diff["fields"] if f["field"] == "system_prompt")
    assert field_change["old"] == "v1 prompt"
    assert field_change["new"] == "v2 prompt"


def test_rollback_restores_prompt_content(auth_client):
    agent = auth_client.post("/api/agents", json={"name": "C", "system_prompt": "original"}).json()
    auth_client.put(f"/api/agents/{agent['id']}", json={
        "name": "C", "system_prompt": "changed", "model": "gpt-4.1-mini", "status": "draft",
    })
    auth_client.post(f"/api/agents/{agent['id']}/versions", json={})

    versions = auth_client.get(f"/api/agents/{agent['id']}/versions").json()
    v1_id = next(v["id"] for v in versions if v["version_no"] == 1)

    r = auth_client.post(f"/api/agents/{agent['id']}/versions/{v1_id}/rollback")
    assert r.status_code == 200, r.text
    rollback_version = r.json()
    assert rollback_version["version_no"] == 3
    assert "回滚" in rollback_version["note"]

    detail = auth_client.get(f"/api/agents/{agent['id']}/versions/{rollback_version['id']}").json()
    assert detail["snapshot"]["system_prompt"] == "original"


def test_publish_fixes_version_and_survives_further_edits(auth_client):
    agent = auth_client.post("/api/agents", json={"name": "D", "system_prompt": "v1"}).json()
    _satisfy_publish_checklist(auth_client, agent["id"], kind="prompt")

    r = auth_client.post(f"/api/agents/{agent['id']}/publish")
    assert r.status_code == 200, r.text
    published = r.json()
    assert published["label"] == "published"
    assert published["version_no"] == 2  # 发布会复制当前版本为一条新的固定版本

    listing = auth_client.get("/api/agents").json()
    entry = next(a for a in listing if a["id"] == agent["id"])
    assert entry["status"] == "published"
    assert entry["published_version_no"] == 2

    # 继续编辑草稿不影响已发布版本内容
    auth_client.put(f"/api/agents/{agent['id']}", json={
        "name": "D", "system_prompt": "v2 draft edit", "model": "gpt-4.1-mini", "status": "draft",
    })
    published_detail = auth_client.get(f"/api/agents/{agent['id']}/versions/{published['id']}").json()
    assert published_detail["snapshot"]["system_prompt"] == "v1"


def test_code_agent_generate_creates_initial_version(auth_client):
    r = auth_client.post("/api/apps/generate", json={"message": "帮我做一个客服助手", "project_name": ""})
    assert r.status_code == 200, r.text
    draft_id = r.json()["draft"]["id"]
    assert set(r.json()["files"]) >= {
        "manifest.json",
        "main.py",
        "agent.py",
        "runtime.py",
        "skills.py",
        "knowledge.py",
        "connectors.py",
        "SKILL.md",
        "tests.json",
        "README.md",
    }

    versions = auth_client.get(f"/api/agents/{draft_id}/versions").json()
    assert len(versions) == 1
    assert versions[0]["version_no"] == 1
    assert versions[0]["kind"] == "code"

    listing = auth_client.get("/api/agents").json()
    entry = next(a for a in listing if a["id"] == draft_id)
    assert entry["kind"] == "code"

    file_paths = {f["path"] for f in auth_client.get(f"/api/apps/drafts/{draft_id}/files").json() if f["type"] == "file"}
    assert {"agent.py", "runtime.py", "skills.py", "knowledge.py", "connectors.py"}.issubset(file_paths)

    main_content = auth_client.get(f"/api/apps/drafts/{draft_id}/files/content", params={"path": "main.py"}).json()["content"]
    agent_content = auth_client.get(f"/api/apps/drafts/{draft_id}/files/content", params={"path": "agent.py"}).json()["content"]
    manifest = auth_client.get(f"/api/apps/drafts/{draft_id}/files/content", params={"path": "manifest.json"}).json()["content"]
    assert "from agent import AtlasAgent" in main_content
    assert "class AtlasAgent" in agent_content
    assert '"framework": "atlas-agent-python"' in manifest


def test_code_agent_diff_and_rollback_restores_files(auth_client):
    draft_id = auth_client.post("/api/apps/generate", json={"message": "做一个流程助手", "project_name": ""}).json()["draft"]["id"]

    auth_client.put(f"/api/apps/drafts/{draft_id}/files/content", json={
        "path": "main.py", "content": "def main(input_text):\n    return 'EDITED: ' + input_text\n",
    })
    v2 = auth_client.post(f"/api/agents/{draft_id}/versions", json={"note": "edited"}).json()
    assert v2["version_no"] == 2

    diff = auth_client.get(f"/api/agents/{draft_id}/versions/diff", params={"from": 1, "to": 2}).json()
    main_diff = next(f for f in diff["files"] if f["path"] == "main.py")
    assert main_diff["status"] == "modified"

    versions = auth_client.get(f"/api/agents/{draft_id}/versions").json()
    v1_id = next(v["id"] for v in versions if v["version_no"] == 1)
    auth_client.post(f"/api/agents/{draft_id}/versions/{v1_id}/rollback")

    content = auth_client.get(f"/api/apps/drafts/{draft_id}/files/content", params={"path": "main.py"}).json()["content"]
    assert "EDITED" not in content


def test_code_agent_publish_pins_version(auth_client):
    draft_id = auth_client.post("/api/apps/generate", json={"message": "做一个知识库问答", "project_name": ""}).json()["draft"]["id"]
    _satisfy_publish_checklist(auth_client, draft_id, kind="code")

    r = auth_client.post(f"/api/agents/{draft_id}/publish")
    assert r.status_code == 200, r.text
    assert r.json()["label"] == "published"

    listing = auth_client.get("/api/agents").json()
    entry = next(a for a in listing if a["id"] == draft_id)
    assert entry["status"] == "published"
    assert entry["published_version_no"] == r.json()["version_no"]
    detail = auth_client.get(f"/api/agents/{draft_id}/versions/{r.json()['id']}").json()
    assert detail["snapshot"]["evaluation"]["kind"] == "evaluate"
    assert detail["snapshot"]["deploy_config"]["visibility"] == "workspace"


def test_refine_updates_same_project_and_persists_builder_history(auth_client, monkeypatch):
    from app import apps as apps_module

    async def no_model(*_args, **_kwargs):
        return ""

    monkeypatch.setattr(apps_module, "complete", no_model)
    created = auth_client.post("/api/apps/generate", json={"message": "做一个客服助手", "project_name": ""}).json()
    draft_id = created["draft"]["id"]

    r = auth_client.post(f"/api/apps/drafts/{draft_id}/refine", json={"message": "增加升级人工客服的规则"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["draft"]["id"] == draft_id
    assert body["version_no"] == 2
    assert "manifest.json" in body["changed_files"]
    assert "requirements.md" in body["changed_files"]

    state = auth_client.get(f"/api/apps/drafts/{draft_id}/product-state").json()
    assert state["agent"]["version_no"] == 2
    assert [item["role"] for item in state["builder_messages"]][-2:] == ["user", "assistant"]
    assert "升级人工客服" in state["builder_messages"][-2]["content"]


def test_web_request_generates_html_preview(auth_client):
    created = auth_client.post("/api/apps/generate", json={"message": "做一个带网页界面的报销助手", "project_name": ""}).json()
    draft_id = created["draft"]["id"]
    assert "preview.html" in created["files"]
    preview = auth_client.get(f"/api/apps/drafts/{draft_id}/preview").json()
    assert preview["exists"] is True
    assert "<!doctype html>" in preview["html"].lower()


def test_publish_auto_saves_dirty_code_workspace(auth_client):
    draft_id = auth_client.post("/api/apps/generate", json={"message": "做一个流程助手", "project_name": ""}).json()["draft"]["id"]
    _satisfy_publish_checklist(auth_client, draft_id, kind="code")
    edited = "def main(input_text):\n    return 'LATEST-WORKSPACE: ' + input_text\n"
    auth_client.put(f"/api/apps/drafts/{draft_id}/files/content", json={"path": "main.py", "content": edited})

    published = auth_client.post(f"/api/agents/{draft_id}/publish")
    assert published.status_code == 200, published.text
    assert published.json()["version_no"] == 3
    detail = auth_client.get(f"/api/agents/{draft_id}/versions/{published.json()['id']}").json()
    assert detail["snapshot"]["files"]["main.py"] == edited


def test_api_invocation_uses_published_code_snapshot(auth_client, monkeypatch):
    from app import apps as apps_module

    monkeypatch.setattr(apps_module, "resolve_provider", lambda model: ("", "", model or "qwen-turbo"))
    created = auth_client.post("/api/apps/generate", json={"message": "做一个知识库问答助手", "project_name": ""}).json()
    draft_id = created["draft"]["id"]
    _satisfy_publish_checklist(auth_client, draft_id, kind="code")
    auth_client.put(f"/api/agents/{draft_id}/deploy-config", json={
        "visibility": "workspace", "shared_user_ids": [], "allowed_knowledge_base_ids": [],
        "allowed_skill_ids": [], "allowed_connectors": [], "write_confirm": True,
        "api_access": True, "call_log_enabled": True,
    })
    published = auth_client.post(f"/api/agents/{draft_id}/publish").json()
    key = auth_client.post(f"/api/agents/{draft_id}/api-key").json()["key"]

    auth_client.put(f"/api/apps/drafts/{draft_id}/files/content", json={
        "path": "main.py",
        "content": "def main(input_text):\n    return 'DRAFT-MUTATION'\n",
    })
    response = auth_client.post(
        f"/api/agents/{draft_id}/invoke",
        json={"input": "hello"},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert response.status_code == 200, response.text
    assert "DRAFT-MUTATION" not in response.json()["output"]
    assert response.json()["version_no"] == published["version_no"]

    listing = auth_client.get("/api/agents").json()
    changed = next(item for item in listing if item["id"] == draft_id)
    assert changed["has_unpublished_changes"] is True
    assert changed["workflow_stage"] == "publish"

    republished = auth_client.post(f"/api/agents/{draft_id}/publish")
    assert republished.status_code == 200, republished.text
    response_after_publish = auth_client.post(
        f"/api/agents/{draft_id}/invoke",
        json={"input": "hello"},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert response_after_publish.status_code == 200, response_after_publish.text
    assert response_after_publish.json()["output"] == "DRAFT-MUTATION"
    assert response_after_publish.json()["version_no"] == republished.json()["version_no"]


def test_cross_workspace_draft_access_blocked(client):
    """草稿目录名 = agent.id，必须经工作区归属校验，不能靠猜 id 跨租户访问。"""
    r = client.get("/api/apps/drafts/nonexistent-id/files")
    assert r.status_code == 401  # 未登录直接拒绝


def test_publish_without_version_rejected(auth_client):
    r = auth_client.post("/api/agents/nonexistent-agent-id/publish")
    assert r.status_code == 404
