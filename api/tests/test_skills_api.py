def test_list_includes_builtins(auth_client):
    r = auth_client.get("/api/skills")
    assert r.status_code == 200
    names = {s["name"] for s in r.json()}
    assert "会议纪要" in names


def test_create_and_get_custom(auth_client):
    r = auth_client.post("/api/skills", json={
        "name": "周报助手", "description": "整理周报", "content": "按完成/在做/计划三段", "trigger_phrases": "周报"})
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    assert r.json()["builtin"] is False
    g = auth_client.get(f"/api/skills/{sid}")
    assert g.status_code == 200 and g.json()["name"] == "周报助手"


def test_builtin_is_readonly(auth_client):
    bid = next(s["id"] for s in auth_client.get("/api/skills").json() if s["builtin"])
    assert auth_client.put(f"/api/skills/{bid}", json={
        "name": "x", "description": "", "content": "", "trigger_phrases": "", "status": "active"}).status_code == 403
    assert auth_client.delete(f"/api/skills/{bid}").status_code == 403


def test_update_and_delete_custom(auth_client):
    sid = auth_client.post("/api/skills", json={
        "name": "t", "description": "", "content": "c", "trigger_phrases": ""}).json()["id"]
    u = auth_client.put(f"/api/skills/{sid}", json={
        "name": "t2", "description": "d", "content": "c2", "trigger_phrases": "x", "status": "active"})
    assert u.status_code == 200 and u.json()["name"] == "t2"
    assert auth_client.delete(f"/api/skills/{sid}").status_code == 200
    assert auth_client.get(f"/api/skills/{sid}").status_code == 404


def test_unauth_blocked(client):
    assert client.get("/api/skills").status_code == 401
