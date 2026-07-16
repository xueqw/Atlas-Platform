def test_skill_discovery_returns_metadata_without_content(auth_client):
    created = auth_client.post("/api/skills", json={
        "name": "Research", "content": "SECRET INSTRUCTION BODY", "category_path": "work/research",
        "summary": "Find and synthesize sources", "use_when": ["research"],
    })
    assert created.status_code == 201, created.text
    response = auth_client.get("/api/skills/discovery")
    assert response.status_code == 200
    body = response.json()
    skill = next(item for item in body["skills"] if item["name"] == "Research")
    assert "content" not in skill
    assert "SECRET" not in str(body)
    assert "work" in body["tree"]["children"]
