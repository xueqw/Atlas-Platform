from datetime import datetime, timedelta, timezone

from app.api_keys import check_key_status, origin_allowed


def _published_agent_with_key(auth_client, name, api_access=True, **key_overrides):
    """建一个 prompt 型 agent，配落地权限（可选开 api_access），发布，生成 key。
    model 显式指定为 qwen-turbo：resolve_provider 对默认的 gpt-4.1-mini 会落到
    openai_base_url（本环境配的是 dashscope 兼容端点），dashscope 不认 gpt-4.1-mini 这个模型名，
    会 404——跟其余聊天类测试一样避开这个环境相关的路由陷阱，不在这个任务里处理。"""
    agent = auth_client.post("/api/agents", json={"name": name, "model": "qwen-turbo"}).json()
    auth_client.put(f"/api/agents/{agent['id']}/deploy-config", json={
        "visibility": "workspace", "shared_user_ids": [], "allowed_knowledge_base_ids": [],
        "allowed_skill_ids": [], "allowed_connectors": [], "write_confirm": True,
        "api_access": True, "call_log_enabled": True,
    })
    r = auth_client.post(f"/api/agents/{agent['id']}/publish")
    assert r.status_code == 200, r.text

    key_resp = auth_client.post(f"/api/agents/{agent['id']}/api-key").json()
    plaintext = key_resp["key"]

    if not api_access:
        auth_client.put(f"/api/agents/{agent['id']}/deploy-config", json={
            "visibility": "workspace", "shared_user_ids": [], "allowed_knowledge_base_ids": [],
            "allowed_skill_ids": [], "allowed_connectors": [], "write_confirm": True,
            "api_access": False, "call_log_enabled": True,
        })

    if key_overrides:
        auth_client.put(f"/api/agents/{agent['id']}/api-key", json={
            "status": "active", "expires_at": None, "daily_quota": None, "allowed_origins": "",
            **key_overrides,
        })

    return agent, plaintext


def test_invoke_missing_key_rejected(auth_client):
    agent, _ = _published_agent_with_key(auth_client, "A")
    r = auth_client.post(f"/api/agents/{agent['id']}/invoke", json={"input": "hi"})
    assert r.status_code == 401, r.text


def test_invoke_wrong_key_rejected(auth_client):
    agent, _ = _published_agent_with_key(auth_client, "B")
    r = auth_client.post(
        f"/api/agents/{agent['id']}/invoke", json={"input": "hi"},
        headers={"Authorization": "Bearer sk-not-a-real-key"},
    )
    assert r.status_code == 401, r.text


def test_invoke_key_from_another_agent_rejected(auth_client):
    agent_a, key_a = _published_agent_with_key(auth_client, "C1")
    agent_b, _ = _published_agent_with_key(auth_client, "C2")

    r = auth_client.post(
        f"/api/agents/{agent_b['id']}/invoke", json={"input": "hi"},
        headers={"Authorization": f"Bearer {key_a}"},
    )
    assert r.status_code == 401, r.text


def test_invoke_disabled_key_rejected(auth_client):
    agent, key = _published_agent_with_key(auth_client, "D", status="disabled")
    r = auth_client.post(
        f"/api/agents/{agent['id']}/invoke", json={"input": "hi"},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert r.status_code == 403, r.text


def test_invoke_expired_key_rejected(auth_client):
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    agent, key = _published_agent_with_key(auth_client, "E", expires_at=past)
    r = auth_client.post(
        f"/api/agents/{agent['id']}/invoke", json={"input": "hi"},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert r.status_code == 403, r.text


def test_invoke_origin_not_allowed_rejected(auth_client):
    agent, key = _published_agent_with_key(auth_client, "F", allowed_origins="https://trusted.example.com")
    r = auth_client.post(
        f"/api/agents/{agent['id']}/invoke", json={"input": "hi"},
        headers={"Authorization": f"Bearer {key}", "Origin": "https://evil.example.com"},
    )
    assert r.status_code == 403, r.text


def test_invoke_api_access_disabled_rejected(auth_client):
    agent, key = _published_agent_with_key(auth_client, "G", api_access=False)
    r = auth_client.post(
        f"/api/agents/{agent['id']}/invoke", json={"input": "hi"},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert r.status_code == 403, r.text


def test_api_key_creation_unpublished_agent_rejected(auth_client):
    agent = auth_client.post("/api/agents", json={"name": "H"}).json()
    auth_client.put(f"/api/agents/{agent['id']}/deploy-config", json={
        "visibility": "workspace", "shared_user_ids": [], "allowed_knowledge_base_ids": [],
        "allowed_skill_ids": [], "allowed_connectors": [], "write_confirm": True,
        "api_access": True, "call_log_enabled": True,
    })
    r = auth_client.post(f"/api/agents/{agent['id']}/api-key")
    assert r.status_code == 400, r.text
    assert "先发布" in r.json()["detail"]


def test_invoke_quota_exceeded_rejected(auth_client):
    agent, key = _published_agent_with_key(auth_client, "I", daily_quota=1)
    headers = {"Authorization": f"Bearer {key}"}

    r1 = auth_client.post(f"/api/agents/{agent['id']}/invoke", json={"input": "hi"}, headers=headers)
    assert r1.status_code == 200, r1.text

    r2 = auth_client.post(f"/api/agents/{agent['id']}/invoke", json={"input": "hi again"}, headers=headers)
    assert r2.status_code == 429, r2.text


def test_invoke_success_returns_output_and_updates_last_used(auth_client):
    agent, key = _published_agent_with_key(auth_client, "J")

    before = auth_client.get(f"/api/agents/{agent['id']}/api-key").json()
    assert before["last_used_at"] is None

    r = auth_client.post(
        f"/api/agents/{agent['id']}/invoke", json={"input": "你好"},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["output"]
    assert body["elapsed_ms"] >= 0

    after = auth_client.get(f"/api/agents/{agent['id']}/api-key").json()
    assert after["last_used_at"] is not None


def test_invoke_x_api_key_header_also_works(auth_client):
    agent, key = _published_agent_with_key(auth_client, "K")
    r = auth_client.post(
        f"/api/agents/{agent['id']}/invoke", json={"input": "hi"},
        headers={"X-Api-Key": key},
    )
    assert r.status_code == 200, r.text


def test_invoke_recorded_as_api_source_in_call_logs(auth_client):
    agent, key = _published_agent_with_key(auth_client, "L")
    auth_client.post(
        f"/api/agents/{agent['id']}/invoke", json={"input": "hi"},
        headers={"Authorization": f"Bearer {key}"},
    )

    logs = auth_client.get(f"/api/agents/{agent['id']}/call-logs").json()
    assert any(entry["source"] == "api" for entry in logs)


# === api_keys 纯函数单测 ===

class _FakeKey:
    def __init__(self, status="active", expires_at=None):
        self.status = status
        self.expires_at = expires_at


def test_check_key_status_active_no_expiry_passes():
    assert check_key_status(_FakeKey()) is None


def test_check_key_status_disabled_rejected():
    assert check_key_status(_FakeKey(status="disabled")) is not None


def test_check_key_status_expired_rejected():
    past = datetime.now(timezone.utc) - timedelta(days=1)
    assert check_key_status(_FakeKey(expires_at=past)) is not None


def test_check_key_status_future_expiry_passes():
    future = datetime.now(timezone.utc) + timedelta(days=1)
    assert check_key_status(_FakeKey(expires_at=future)) is None


def test_origin_allowed_empty_allowlist_means_unrestricted():
    assert origin_allowed("", "https://anything.example.com") is True
    assert origin_allowed("", None) is True


def test_origin_allowed_matches_list():
    assert origin_allowed("https://a.com, https://b.com", "https://b.com") is True
    assert origin_allowed("https://a.com, https://b.com", "https://c.com") is False
