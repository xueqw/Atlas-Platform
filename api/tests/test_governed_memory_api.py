def test_governed_fact_api_is_idempotent_and_tombstoned(auth_client):
    agent = auth_client.post("/api/agents", json={"name": "Governed memory"}).json()
    path = f"/api/agents/{agent['id']}/memory/facts"
    payload = {
        "subject": "customer", "predicate": "prefers", "object_value": "email",
        "idempotency_key": "governed-fact-1", "evidence": "explicit request",
    }
    first = auth_client.post(path, json=payload)
    assert first.status_code == 201, first.text
    second = auth_client.post(path, json=payload)
    assert second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    facts = auth_client.get(path).json()
    assert facts["untrusted_context"] is True
    assert len(facts["facts"]) == 1
    deleted = auth_client.request("DELETE", f"{path}/{first.json()['id']}", json={"idempotency_key": "governed-delete-1"})
    assert deleted.status_code == 200
    assert auth_client.get(path).json()["facts"] == []


def test_structured_profile_card_api_has_version_conflict_and_delete(auth_client):
    agent = auth_client.post("/api/agents", json={"name": "Profile memory"}).json()
    path = f"/api/agents/{agent['id']}/memory/profile"
    first = auth_client.put(path, json={
        "fields": {"locale": "zh-CN", "home_city": "Hangzhou"},
        "expected_version": 0,
        "idempotency_key": "profile-create",
    })
    assert first.status_code == 200, first.text
    assert first.json()["version"] == 1
    assert auth_client.get(path).json()["fields"]["home_city"] == "Hangzhou"
    conflict = auth_client.put(path, json={
        "fields": {"home_city": "Shanghai"},
        "expected_version": 0,
        "idempotency_key": "profile-stale",
    })
    assert conflict.status_code == 409
    deleted = auth_client.request("DELETE", path, json={
        "expected_version": 1,
        "idempotency_key": "profile-delete",
    })
    assert deleted.status_code == 200, deleted.text
    assert auth_client.get(path).json() == {"version": 0, "fields": {}}


def test_delete_all_memory_tombstones_governed_facts_and_profile(auth_client):
    agent = auth_client.post("/api/agents", json={"name": "Forget me"}).json()
    base = f"/api/agents/{agent['id']}/memory"
    fact = auth_client.post(f"{base}/facts", json={
        "subject": "user", "predicate": "city", "object_value": "Hangzhou",
        "idempotency_key": "bulk-fact",
    })
    assert fact.status_code == 201
    profile = auth_client.put(f"{base}/profile", json={
        "fields": {"home_city": "Hangzhou"},
        "expected_version": 0,
        "idempotency_key": "bulk-profile",
    })
    assert profile.status_code == 200
    deleted = auth_client.delete(base)
    assert deleted.status_code == 204
    assert auth_client.get(f"{base}/facts").json()["facts"] == []
    assert auth_client.get(f"{base}/profile").json() == {"version": 0, "fields": {}}


def test_short_memory_is_durable_versioned_and_rebuilt_after_hot_cache_loss(auth_client):
    from app.state_store import state_store

    agent = auth_client.post("/api/agents", json={"name": "Durable session"}).json()
    base = f"/api/agents/{agent['id']}/memory/short"
    first = auth_client.put(base, json={
        "conversation_id": "conversation-1",
        "messages": [{"role": "user", "content": "remember this"}],
        "expected_version": 0,
        "idempotency_key": "session-create-1",
    })
    assert first.status_code == 200, first.text
    assert first.json()["version"] == 1

    # Simulate Redis loss. The API must rebuild the exact tenant/user/agent
    # session from its durable checkpoint and repopulate the hot cache.
    state_store.delete_prefix("short-memory", "")
    rebuilt = auth_client.get(base, params={"conversation_id": "conversation-1"})
    assert rebuilt.status_code == 200
    assert rebuilt.json()["source"] == "postgresql"
    assert rebuilt.json()["version"] == 1
    assert rebuilt.json()["messages"][0]["content"] == "remember this"

    stale = auth_client.put(base, json={
        "conversation_id": "conversation-1", "messages": [],
        "expected_version": 0, "idempotency_key": "session-stale-1",
    })
    assert stale.status_code == 409

    deleted = auth_client.request("DELETE", base, json={
        "conversation_id": "conversation-1", "expected_version": 1,
        "idempotency_key": "session-delete-1",
    })
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["version"] == 2
    after_delete = auth_client.get(base, params={"conversation_id": "conversation-1"})
    assert after_delete.json()["messages"] == []
