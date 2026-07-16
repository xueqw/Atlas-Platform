from pathlib import Path
import os
import subprocess

from sqlalchemy import select


def test_knowledge_upload_persists_original(auth_client):
    from app.database import SessionLocal
    from app.models import Document
    from app.object_storage import object_storage

    kb = auth_client.post("/api/knowledge-bases", json={"name": "原文件测试", "description": ""}).json()
    response = auth_client.post(
        f"/api/knowledge-bases/{kb['id']}/documents",
        files={"file": ("policy.txt", b"Atlas production knowledge document", "text/plain")},
    )
    assert response.status_code == 201, response.text
    with SessionLocal() as db:
        row = db.scalar(select(Document).where(Document.id == response.json()["id"]))
        assert row and row.object_key.startswith("workspaces/")
        assert object_storage.get(row.object_key) == b"Atlas production knowledge document"


def test_agent_memory_is_private_per_user(auth_client, monkeypatch):
    from app import memory

    async def no_embedding(_text):
        return None

    monkeypatch.setattr(memory, "embed_query", no_embedding)
    agent = auth_client.post("/api/agents", json={"name": "Memory Agent"}).json()
    created = auth_client.post(
        f"/api/agents/{agent['id']}/memory/long",
        json={"content": "我偏好简洁的回答", "category": "preference"},
    )
    assert created.status_code == 201, created.text
    assert len(auth_client.get(f"/api/agents/{agent['id']}/memory/long").json()) == 1

    auth_client.post("/api/auth/logout")
    login = auth_client.post("/api/auth/login", json={"username": "alice", "password": "atlas123"})
    assert login.status_code == 200
    assert auth_client.get(f"/api/agents/{agent['id']}/memory/long").json() == []


def test_short_memory_round_trip(auth_client):
    agent = auth_client.post("/api/agents", json={"name": "Short Memory Agent"}).json()
    payload = {"conversation_id": "conversation-1", "messages": [{"role": "user", "content": "hello"}]}
    assert auth_client.put(f"/api/agents/{agent['id']}/memory/short", json=payload).status_code == 200
    response = auth_client.get(
        f"/api/agents/{agent['id']}/memory/short", params={"conversation_id": "conversation-1"},
    )
    assert response.json()["messages"] == payload["messages"]
    assert auth_client.delete(f"/api/agents/{agent['id']}/memory").status_code == 204
    response = auth_client.get(
        f"/api/agents/{agent['id']}/memory/short", params={"conversation_id": "conversation-1"},
    )
    assert response.json()["messages"] == []


def test_connector_credentials_are_encrypted_at_rest(client):
    from sqlalchemy import text
    from app.database import SessionLocal, engine
    from app.models import ConnectorConfig

    with SessionLocal() as db:
        row = ConnectorConfig(provider="encrypted-test", data='{"api_key":"secret-value"}')
        db.merge(row)
        db.commit()
    with engine.connect() as conn:
        raw = conn.execute(text("SELECT data FROM connector_configs WHERE provider='encrypted-test'")).scalar_one()
    assert raw.startswith("enc:v1:")
    assert "secret-value" not in raw
    with SessionLocal() as db:
        assert db.get(ConnectorConfig, "encrypted-test").data == '{"api_key":"secret-value"}'


def test_docker_runner_has_security_limits(tmp_path, monkeypatch):
    from app import code_runner

    (tmp_path / ".atlas_runner.py").write_text("print('ok')", encoding="utf-8")
    monkeypatch.setattr(code_runner.settings, "code_runner_backend", "docker")
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        return subprocess.CompletedProcess(command, 0, "ok\n", "")

    monkeypatch.setattr(code_runner.subprocess, "run", fake_run)
    result = code_runner.run_python(tmp_path, 10)
    command = captured["command"]
    assert result.returncode == 0
    assert ["--network", "none"] == command[command.index("--network"):command.index("--network") + 2]
    assert "--read-only" in command
    assert ["--cap-drop", "ALL"] == command[command.index("--cap-drop"):command.index("--cap-drop") + 2]
    assert "no-new-privileges" in command
    assert command[command.index("--user") + 1] == f"{getattr(os, 'getuid', lambda: 1000)()}:{getattr(os, 'getgid', lambda: 1000)()}"
    assert command[command.index("--mount") + 1].endswith("readonly")


def test_preview_html_injects_restrictive_csp():
    from app.apps import secure_preview_html

    secured = secure_preview_html("<html><head><title>x</title></head><body></body></html>")
    assert "Content-Security-Policy" in secured
    assert "connect-src 'none'" in secured
    assert "form-action 'none'" in secured
