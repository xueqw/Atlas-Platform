import json


def test_application_development_product_flow(auth_client, monkeypatch):
    from app import apps as apps_module

    async def no_coding_model(*_args, **_kwargs):
        return ""

    monkeypatch.setattr(apps_module, "complete", no_coding_model)
    monkeypatch.setattr(apps_module, "resolve_provider", lambda model: ("", "", model or "qwen-turbo"))

    created = auth_client.post("/api/apps/generate", json={
        "message": "做一个带网页界面的企业制度问答助手",
        "project_name": "",
    })
    assert created.status_code == 200, created.text
    draft = created.json()["draft"]
    draft_id = draft["id"]
    assert "preview.html" in created.json()["files"]

    refined = auth_client.post(f"/api/apps/drafts/{draft_id}/refine", json={
        "message": "回答时必须给出下一步办理建议",
    })
    assert refined.status_code == 200, refined.text
    assert refined.json()["draft"]["id"] == draft_id
    assert refined.json()["version_no"] == 2

    knowledge = auth_client.post("/api/knowledge-bases", json={
        "name": "企业制度库",
        "description": "用于应用开发闭环测试",
    }).json()
    skills = auth_client.get("/api/skills").json()
    selected_skill = skills[0]
    manifest_response = auth_client.get(
        f"/api/apps/drafts/{draft_id}/files/content",
        params={"path": "manifest.json"},
    ).json()
    manifest = json.loads(manifest_response["content"])
    manifest["knowledge_bases"] = [knowledge["id"]]
    manifest["skills"] = [selected_skill["name"]]
    auth_client.put(f"/api/apps/drafts/{draft_id}/files/content", json={
        "path": "manifest.json",
        "content": json.dumps(manifest, ensure_ascii=False, indent=2),
    })

    preview = auth_client.get(f"/api/apps/drafts/{draft_id}/preview").json()
    assert preview["exists"] is True
    assert draft["name"] in preview["html"]

    test_run = auth_client.post(f"/api/apps/drafts/{draft_id}/run", json={
        "input_text": "报销需要哪些材料？",
    })
    assert test_run.status_code == 200, test_run.text
    assert test_run.json()["ok"] is True
    assert any(step["type"] == "retrieve" for step in test_run.json()["trace"])
    assert any(step["type"] == "skill" for step in test_run.json()["trace"])

    evaluation = auth_client.post(f"/api/apps/drafts/{draft_id}/evaluate", json={
        "cases": [
            {"name": "制度问答", "input": "报销需要哪些材料？", "expected": ""},
            {"name": "办理建议", "input": "我下一步应该做什么？", "expected": ""},
        ],
    })
    assert evaluation.status_code == 200, evaluation.text
    assert evaluation.json()["total"] == 2
    assert "metrics" in evaluation.json()["summary"]

    deploy = auth_client.put(f"/api/agents/{draft_id}/deploy-config", json={
        "visibility": "workspace",
        "shared_user_ids": [],
        "allowed_knowledge_base_ids": [knowledge["id"]],
        "allowed_skill_ids": [selected_skill["id"]],
        "allowed_connectors": [],
        "write_confirm": True,
        "api_access": True,
        "call_log_enabled": True,
        "high_risk_approved": False,
    })
    assert deploy.status_code == 200, deploy.text

    before_publish = auth_client.get(f"/api/apps/drafts/{draft_id}/product-state").json()
    assert before_publish["latest_test"]["ok"] is True
    assert before_publish["latest_evaluation"]["total"] == 2
    assert before_publish["publish_checklist"]["can_publish"] is True
    assert len(before_publish["builder_messages"]) >= 4

    published = auth_client.post(f"/api/agents/{draft_id}/publish")
    assert published.status_code == 200, published.text
    published_version = published.json()["version_no"]
    after_publish = auth_client.get(f"/api/apps/drafts/{draft_id}/product-state").json()
    assert after_publish["agent"]["status"] == "published"
    assert after_publish["agent"]["workflow_stage"] == "published"
    assert after_publish["agent"]["has_unpublished_changes"] is False

    key_response = auth_client.post(f"/api/agents/{draft_id}/api-key")
    assert key_response.status_code == 201, key_response.text
    api_key = key_response.json()["key"]
    invocation = auth_client.post(
        f"/api/agents/{draft_id}/invoke",
        json={"input": "请介绍你的能力"},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert invocation.status_code == 200, invocation.text
    assert invocation.json()["version_no"] == published_version

    auth_client.put(f"/api/apps/drafts/{draft_id}/files/content", json={
        "path": "main.py",
        "content": "def main(input_text):\n    return 'NEXT-DRAFT'\n",
    })
    changed_state = auth_client.get(f"/api/apps/drafts/{draft_id}/product-state").json()
    assert changed_state["agent"]["workflow_stage"] == "publish"
    assert changed_state["agent"]["has_unpublished_changes"] is True

    still_old = auth_client.post(
        f"/api/agents/{draft_id}/invoke",
        json={"input": "hello"},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert still_old.status_code == 200, still_old.text
    assert "NEXT-DRAFT" not in still_old.json()["output"]

    republished = auth_client.post(f"/api/agents/{draft_id}/publish")
    assert republished.status_code == 200, republished.text
    new_online = auth_client.post(
        f"/api/agents/{draft_id}/invoke",
        json={"input": "hello"},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert new_online.status_code == 200, new_online.text
    assert new_online.json()["output"] == "NEXT-DRAFT"
    assert new_online.json()["version_no"] == republished.json()["version_no"]

    logs = auth_client.get(f"/api/agents/{draft_id}/call-logs").json()
    assert any(item["source"] == "api" and item["request_path"].endswith("/invoke") for item in logs)
