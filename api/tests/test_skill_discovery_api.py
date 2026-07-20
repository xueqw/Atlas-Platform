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


def test_skill_discovery_search_filter_sort_and_pagination(auth_client):
    for name, category in (("Alpha research", "work/research"), ("Beta writing", "work/writing")):
        response = auth_client.post("/api/skills", json={
            "name": name,
            "content": f"private {name}",
            "category_path": category,
            "summary": name,
            "use_when": [name.lower()],
        })
        assert response.status_code == 201, response.text

    response = auth_client.get(
        "/api/skills/discovery",
        params={"category": "work", "query": "research", "sort_by": "name", "descending": "false", "page_size": 1},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["page"] == 1
    assert body["page_size"] == 1
    assert body["total"] >= 1
    assert len(body["items"]) == 1
    assert "content" not in body["items"][0]
    assert "private" not in str(body)


def test_skill_create_validates_all_governed_metadata(auth_client):
    invalid_version = auth_client.post("/api/skills", json={
        "name": "Invalid", "version": "latest", "category_path": "work/research",
    })
    assert invalid_version.status_code == 422
    invalid_schema = auth_client.post("/api/skills", json={
        "name": "Invalid schema", "category_path": "work/research",
        "input_schema": {"type": "array"},
    })
    assert invalid_schema.status_code == 422
    incomplete_published = auth_client.post("/api/skills", json={
        "name": "Published too soon", "status": "published", "category_path": "work/research",
    })
    assert incomplete_published.status_code == 422


def test_progressive_content_requires_same_user_audited_selection(auth_client):
    from app.database import SessionLocal
    from app.memory_ledger import MemoryScope
    from app.skill_router import persist_router_decision

    created = auth_client.post("/api/skills", json={
        "name": "Selected", "content": "selected private body", "category_path": "work/research",
    }).json()
    denied = auth_client.get(
        f"/api/skills/discovery/{created['id']}/content", params={"decision_id": "missing"},
    )
    assert denied.status_code == 403

    with SessionLocal() as db:
        me = auth_client.get("/api/me").json()
        user_id = me["user"]["id"]
        workspace_id = me["workspace"]["id"]
        record = persist_router_decision(
            db,
            scope=MemoryScope(workspace_id=workspace_id, user_id=user_id, agent_id="agent-test"),
            decision={
                "query": "selected", "selected_ids": [created["id"]], "decision": "selected",
                "reasons": ["test"], "recall": [], "rerank": [],
            },
        )
        decision_id = record.decision_id
        db.commit()
    allowed = auth_client.get(
        f"/api/skills/discovery/{created['id']}/content", params={"decision_id": decision_id},
    )
    assert allowed.status_code == 200
    assert allowed.json()["content"] == "selected private body"
