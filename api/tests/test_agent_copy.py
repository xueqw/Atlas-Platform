def test_copy_nonexistent_agent_returns_404(auth_client):
    r = auth_client.post("/api/agents/nonexistent-id/copy")
    assert r.status_code == 404


def test_copy_prompt_agent_duplicates_content_with_new_id(auth_client):
    source = auth_client.post("/api/agents", json={
        "name": "客服助手", "description": "desc", "system_prompt": "你是客服助手", "model": "qwen-turbo",
    }).json()

    r = auth_client.post(f"/api/agents/{source['id']}/copy")
    assert r.status_code == 201, r.text
    copy = r.json()

    assert copy["id"] != source["id"]
    assert copy["name"] == "客服助手（副本）"
    assert copy["system_prompt"] == "你是客服助手"
    assert copy["status"] == "draft"
    assert copy["version_no"] == 1


def test_copy_published_agent_creates_independent_draft(auth_client):
    source = auth_client.post("/api/agents", json={"name": "已发布助手"}).json()
    auth_client.put(f"/api/agents/{source['id']}/deploy-config", json={
        "visibility": "workspace", "shared_user_ids": [], "allowed_knowledge_base_ids": [],
        "allowed_skill_ids": [], "allowed_connectors": [], "write_confirm": True,
        "api_access": False, "call_log_enabled": True,
    })
    auth_client.post(f"/api/agents/{source['id']}/publish")

    r = auth_client.post(f"/api/agents/{source['id']}/copy")
    assert r.status_code == 201, r.text
    copy = r.json()
    assert copy["status"] == "draft"
    assert copy["published_version_no"] is None


def test_copy_code_agent_duplicates_files_independently(auth_client):
    source_draft = auth_client.post("/api/apps/generate", json={
        "message": "帮我做一个流程办理助手", "project_name": "",
    }).json()["draft"]

    r = auth_client.post(f"/api/agents/{source_draft['id']}/copy")
    assert r.status_code == 201, r.text
    copy = r.json()
    assert copy["kind"] == "code"

    source_files = {f["path"]: f for f in auth_client.get(f"/api/apps/drafts/{source_draft['id']}/files").json() if f["type"] == "file"}
    copy_files = {f["path"]: f for f in auth_client.get(f"/api/apps/drafts/{copy['id']}/files").json() if f["type"] == "file"}
    assert set(source_files) == set(copy_files)

    source_main = auth_client.get(f"/api/apps/drafts/{source_draft['id']}/files/content", params={"path": "main.py"}).json()["content"]
    copy_main = auth_client.get(f"/api/apps/drafts/{copy['id']}/files/content", params={"path": "main.py"}).json()["content"]
    assert source_main == copy_main

    # 编辑副本文件不应污染源文件——目录互相独立
    auth_client.put(f"/api/apps/drafts/{copy['id']}/files/content", json={"path": "main.py", "content": "print('changed')"})
    source_main_after = auth_client.get(f"/api/apps/drafts/{source_draft['id']}/files/content", params={"path": "main.py"}).json()["content"]
    assert source_main_after == source_main
