def test_list_agent_templates_returns_five(auth_client):
    r = auth_client.get("/api/agents/templates")
    assert r.status_code == 200, r.text
    items = r.json()
    assert len(items) == 5
    for item in items:
        assert set(item.keys()) == {"id", "name", "description"}


def test_create_from_nonexistent_template_returns_404(auth_client):
    r = auth_client.post("/api/agents/from-template", json={"template_id": "does-not-exist"})
    assert r.status_code == 404


def test_create_from_template_uses_template_content(auth_client):
    templates = auth_client.get("/api/agents/templates").json()
    template_id = templates[0]["id"]

    r = auth_client.post("/api/agents/from-template", json={"template_id": template_id})
    assert r.status_code == 201, r.text
    agent = r.json()
    assert agent["kind"] == "prompt"
    assert agent["status"] == "draft"
    assert agent["version_no"] == 1
    assert agent["name"] == templates[0]["name"]

    version = auth_client.get(f"/api/agents/{agent['id']}/versions").json()[0]
    detail = auth_client.get(f"/api/agents/{agent['id']}/versions/{version['id']}").json()
    assert detail["snapshot"]["system_prompt"]
