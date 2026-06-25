import json
from app.database import SessionLocal
from app.models import WorkflowRun


def _stream_events(auth_client, cid, body):
    events = []
    with auth_client.stream("POST", f"/api/conversations/{cid}/messages/stream", json=body) as s:
        for line in s.iter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


def test_manual_skill_forced_and_persisted(auth_client):
    sid = next(s["id"] for s in auth_client.get("/api/skills").json() if s["name"] == "会议纪要")
    cid = auth_client.post("/api/conversations", json={"title": "t"}).json()["id"]
    events = _stream_events(auth_client, cid, {
        "content": "hello", "knowledge_base_id": None, "agent_id": None, "model": None,
        "connectors": [], "attachment_name": None, "attachment_text": None, "skill_ids": [sid]})
    sel = next((e for e in events if e["type"] == "skills_selected"), None)
    assert sel is not None
    assert sid in [s["id"] for s in sel["skills"]]
    run_id = next(e["run_id"] for e in events if e["type"] == "run_started")
    with SessionLocal() as db:
        run = db.get(WorkflowRun, run_id)
        plan = json.loads(run.plan_json)
    assert sid in [s["id"] for s in plan["skills"]]
    assert any(s["source"] == "manual" for s in plan["skills"] if s["id"] == sid)
