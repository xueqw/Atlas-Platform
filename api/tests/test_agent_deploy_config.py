from app.deploy_policy import apply_resource_permissions, check_visibility, parse_deploy_config
from fastapi.testclient import TestClient
from app.main import app


def test_default_deploy_config_shape(auth_client):
    agent = auth_client.post("/api/agents", json={"name": "客服助手"}).json()
    r = auth_client.get(f"/api/agents/{agent['id']}/deploy-config")
    assert r.status_code == 200, r.text
    config = r.json()
    assert config["visibility"] == "workspace"
    assert config["write_confirm"] is True
    assert config["call_log_enabled"] is True
    assert config["api_access"] is False


def test_update_deploy_config_persists(auth_client):
    agent = auth_client.post("/api/agents", json={"name": "A"}).json()
    payload = {
        "visibility": "private",
        "shared_user_ids": [],
        "allowed_knowledge_base_ids": [],
        "allowed_skill_ids": [],
        "allowed_connectors": ["feishu"],
        "write_confirm": False,
        "api_access": True,
        "call_log_enabled": False,
    }
    r = auth_client.put(f"/api/agents/{agent['id']}/deploy-config", json=payload)
    assert r.status_code == 200, r.text

    r = auth_client.get(f"/api/agents/{agent['id']}/deploy-config")
    config = r.json()
    assert config["visibility"] == "private"
    assert config["allowed_connectors"] == ["feishu"]
    assert config["write_confirm"] is False
    assert config["api_access"] is True
    assert config["call_log_enabled"] is False


def test_call_log_disabled_hides_logs_and_zeroes_count(auth_client):
    agent = auth_client.post("/api/agents", json={"name": "B"}).json()
    payload = {
        "visibility": "workspace", "shared_user_ids": [], "allowed_knowledge_base_ids": [],
        "allowed_skill_ids": [], "allowed_connectors": [], "write_confirm": True,
        "api_access": False, "call_log_enabled": False,
    }
    auth_client.put(f"/api/agents/{agent['id']}/deploy-config", json=payload)

    r = auth_client.get(f"/api/agents/{agent['id']}/call-logs")
    assert r.status_code == 200, r.text
    assert r.json() == []

    listing = auth_client.get("/api/agents").json()
    entry = next(a for a in listing if a["id"] == agent["id"])
    assert entry["call_count"] == 0


def test_workspace_members_endpoint(auth_client):
    r = auth_client.get("/api/workspace/members")
    assert r.status_code == 200, r.text
    usernames = {m["username"] for m in r.json()}
    assert "admin" in usernames
    assert "alice" in usernames  # 预置测试账号同工作区


def test_cross_user_private_agent_blocked():
    """两个不同账号（同工作区）：private 可见范围下，非创建者调用被拒。"""
    with TestClient(app) as admin_client:
        r = admin_client.post("/api/auth/login", json={"username": "admin", "password": "atlas123"})
        assert r.status_code == 200, r.text

        agent = admin_client.post("/api/agents", json={"name": "Private Agent"}).json()
        payload = {
            "visibility": "private", "shared_user_ids": [], "allowed_knowledge_base_ids": [],
            "allowed_skill_ids": [], "allowed_connectors": [], "write_confirm": True,
            "api_access": False, "call_log_enabled": True,
        }
        admin_client.put(f"/api/agents/{agent['id']}/deploy-config", json=payload)

        conversation = admin_client.post("/api/conversations", json={"title": "t"}).json()

    with TestClient(app) as alice_client:
        r = alice_client.post("/api/auth/login", json={"username": "alice", "password": "atlas123"})
        assert r.status_code == 200, r.text

        r = alice_client.post(
            f"/api/conversations/{conversation['id']}/messages/stream",
            json={"content": "hello", "agent_id": agent["id"]},
        )
        assert r.status_code == 403, r.text


def test_private_agent_creator_can_still_use_it():
    with TestClient(app) as admin_client:
        r = admin_client.post("/api/auth/login", json={"username": "admin", "password": "atlas123"})
        assert r.status_code == 200, r.text

        agent = admin_client.post("/api/agents", json={"name": "Own Private Agent"}).json()
        payload = {
            "visibility": "private", "shared_user_ids": [], "allowed_knowledge_base_ids": [],
            "allowed_skill_ids": [], "allowed_connectors": [], "write_confirm": True,
            "api_access": False, "call_log_enabled": True,
        }
        admin_client.put(f"/api/agents/{agent['id']}/deploy-config", json=payload)

        conversation = admin_client.post("/api/conversations", json={"title": "t"}).json()
        r = admin_client.post(
            f"/api/conversations/{conversation['id']}/messages/stream",
            json={"content": "hello", "agent_id": agent["id"]},
        )
        assert r.status_code == 200, r.text


# === deploy_policy 纯函数单测 ===

def test_parse_deploy_config_defaults_on_bad_json():
    config = parse_deploy_config("not json")
    assert config["visibility"] == "workspace"
    assert config["write_confirm"] is True


def test_parse_deploy_config_merges_with_defaults():
    config = parse_deploy_config('{"visibility": "private"}')
    assert config["visibility"] == "private"
    assert config["write_confirm"] is True  # 缺失字段回退默认值


def test_check_visibility_private_blocks_non_creator():
    config = parse_deploy_config('{"visibility": "private"}')
    assert check_visibility("user-a", config, "user-a") is True
    assert check_visibility("user-a", config, "user-b") is False


def test_check_visibility_shared_checks_list():
    config = parse_deploy_config('{"visibility": "shared", "shared_user_ids": ["user-b"]}')
    assert check_visibility("user-a", config, "user-b") is True
    assert check_visibility("user-a", config, "user-c") is False


def test_check_visibility_workspace_allows_everyone():
    config = parse_deploy_config('{"visibility": "workspace"}')
    assert check_visibility("user-a", config, "user-z") is True


def test_check_visibility_no_creator_is_backward_compatible():
    """历史 Agent 没有 created_by：任何配置下都放行，不因这次改动破坏存量行为。"""
    config = parse_deploy_config('{"visibility": "private"}')
    assert check_visibility(None, config, "anyone") is True


def test_apply_resource_permissions_empty_allowlist_means_unrestricted():
    config = parse_deploy_config("{}")
    connectors, skills, kb_id = apply_resource_permissions(config, ["feishu", "github"], [{"id": "s1"}], "kb-1")
    assert connectors == ["feishu", "github"]
    assert skills == [{"id": "s1"}]
    assert kb_id == "kb-1"


def test_apply_resource_permissions_narrows_when_allowlist_set():
    config = parse_deploy_config('{"allowed_connectors": ["feishu"], "allowed_skill_ids": ["s1"], "allowed_knowledge_base_ids": ["kb-1"]}')
    connectors, skills, kb_id = apply_resource_permissions(
        config, ["feishu", "github"], [{"id": "s1"}, {"id": "s2"}], "kb-2",
    )
    assert connectors == ["feishu"]
    assert skills == [{"id": "s1"}]
    assert kb_id is None  # kb-2 不在允许列表里，被收窄为不使用
