def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_login_seeds_admin(auth_client):
    r = auth_client.get("/api/me")
    assert r.status_code == 200
    assert r.json()["user"]["username"] == "admin"
