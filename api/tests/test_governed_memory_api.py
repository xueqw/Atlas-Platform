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
