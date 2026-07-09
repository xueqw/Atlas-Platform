def _published_agent(auth_client, name):
    agent = auth_client.post("/api/agents", json={"name": name}).json()
    auth_client.put(f"/api/agents/{agent['id']}/deploy-config", json={
        "visibility": "workspace", "shared_user_ids": [], "allowed_knowledge_base_ids": [],
        "allowed_skill_ids": [], "allowed_connectors": [], "write_confirm": True,
        "api_access": False, "call_log_enabled": True,
    })
    r = auth_client.post(f"/api/agents/{agent['id']}/publish")
    assert r.status_code == 200, r.text
    return agent


def test_archive_requires_published_status(auth_client):
    agent = auth_client.post("/api/agents", json={"name": "A"}).json()  # 草稿，未发布
    r = auth_client.post(f"/api/agents/{agent['id']}/archive")
    assert r.status_code == 400, r.text


def test_archive_published_agent_flips_status(auth_client):
    agent = _published_agent(auth_client, "B")
    r = auth_client.post(f"/api/agents/{agent['id']}/archive")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "archived"


def test_unarchive_requires_archived_status(auth_client):
    agent = _published_agent(auth_client, "C")
    r = auth_client.post(f"/api/agents/{agent['id']}/unarchive")
    assert r.status_code == 400, r.text  # 还是 published，不是 archived


def test_unarchive_restores_published_status(auth_client):
    agent = _published_agent(auth_client, "D")
    auth_client.post(f"/api/agents/{agent['id']}/archive")

    r = auth_client.post(f"/api/agents/{agent['id']}/unarchive")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "published"


def test_archive_does_not_touch_current_version(auth_client):
    """下架/重新上架只是状态开关，不该动版本号或触碰发布前检查。"""
    agent = _published_agent(auth_client, "E")
    before = auth_client.get("/api/agents").json()
    entry_before = next(a for a in before if a["id"] == agent["id"])

    auth_client.post(f"/api/agents/{agent['id']}/archive")
    auth_client.post(f"/api/agents/{agent['id']}/unarchive")

    after = auth_client.get("/api/agents").json()
    entry_after = next(a for a in after if a["id"] == agent["id"])
    assert entry_after["version_no"] == entry_before["version_no"]
    assert entry_after["published_version_no"] == entry_before["published_version_no"]


def test_archived_agent_rejected_in_chat(auth_client):
    agent = _published_agent(auth_client, "F")
    auth_client.post(f"/api/agents/{agent['id']}/archive")

    conversation = auth_client.post("/api/conversations", json={"title": "t"}).json()
    r = auth_client.post(
        f"/api/conversations/{conversation['id']}/messages/stream",
        json={"content": "hello", "agent_id": agent["id"]},
    )
    assert r.status_code == 403, r.text


def test_archived_agent_rejected_by_invoke(auth_client):
    agent = _published_agent(auth_client, "G")
    auth_client.put(f"/api/agents/{agent['id']}/deploy-config", json={
        "visibility": "workspace", "shared_user_ids": [], "allowed_knowledge_base_ids": [],
        "allowed_skill_ids": [], "allowed_connectors": [], "write_confirm": True,
        "api_access": True, "call_log_enabled": True,
    })
    key = auth_client.post(f"/api/agents/{agent['id']}/api-key").json()["key"]
    auth_client.post(f"/api/agents/{agent['id']}/archive")

    r = auth_client.post(
        f"/api/agents/{agent['id']}/invoke", json={"input": "hi"},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert r.status_code == 403, r.text


def test_archive_agent_not_found(auth_client):
    r = auth_client.post("/api/agents/nonexistent-id/archive")
    assert r.status_code == 404
