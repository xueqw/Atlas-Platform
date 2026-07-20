import json
import asyncio
import threading

from sqlalchemy import select

from app.database import SessionLocal
from app.governance_models import GovernanceAuditRecord, OrchestrationRunRecord
from app.models import Agent, RuntimeEvent, RuntimeRun


def test_organizer_creates_ephemeral_worker_and_executes_it(auth_client, monkeypatch):
    agent = auth_client.post("/api/agents", json={"name": "Organizer"}).json()
    calls = 0

    async def model(messages, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return ""  # Exercise the bounded deterministic planning fallback.
        payload = json.loads(messages[-1]["content"])
        return json.dumps({"answer": f"completed:{payload['task_id']}"})

    monkeypatch.setattr("app.multi_agent_api.complete", model)
    planned = auth_client.post("/api/orchestrations/plan", json={
        "agent_id": agent["id"],
        "goal": "Prepare a support summary",
        "max_workers": 2,
        "concurrency": 2,
    })
    assert planned.status_code == 201, planned.text
    body = planned.json()
    assert body["planner_mode"] == "deterministic_fallback"
    assert body["workers"][0]["lifecycle"] == "ephemeral"
    assert body["tasks"][0]["worker_id"] == body["workers"][0]["worker_id"]

    executed = auth_client.post(f"/api/orchestrations/{body['run_id']}/execute")
    assert executed.status_code == 200, executed.text
    result = executed.json()
    assert result["status"] == "succeeded"
    assert result["aggregate"]["results"][0]["result"]["answer"] == "completed:primary-task"
    with SessionLocal() as db:
        runtime = db.scalar(select(RuntimeRun).where(
            RuntimeRun.id == result["aggregate"]["results"][0]["result"].get("runtime_run_id", "")
        ))
        # The public aggregate intentionally keeps runtime metadata beside the
        # envelope, so query by immutable source/version instead.
        runtime = db.scalar(select(RuntimeRun).where(
            RuntimeRun.source == "subagent",
            RuntimeRun.agent_id == agent["id"],
        ).order_by(RuntimeRun.created_at.desc()))
        assert runtime is not None
        assert runtime.version_id == body["scheduler_state"]["parent_agent_version_id"]
        assert db.scalar(select(RuntimeEvent.id).where(RuntimeEvent.run_id == runtime.id)) is not None


def test_organizer_intersects_model_tools_with_trusted_allowlist(auth_client, monkeypatch):
    agent = auth_client.post("/api/agents", json={"name": "Tool Organizer"}).json()
    proposal = {
        "workers": [{
            "worker_id": "researcher", "role": "researcher", "objective": "research",
            "input_schema": {"type": "object", "required": ["goal"],
                             "properties": {"goal": {"type": "string"}}},
            "output_schema": {"type": "object", "required": ["answer"],
                              "properties": {"answer": {"type": "string"}}},
            "allowed_tools": ["search", "dangerous_write"],
        }],
        "tasks": [{
            "task_id": "research", "worker_id": "researcher",
            "payload": {"goal": "research"}, "depends_on": [],
        }],
    }

    async def model(*_args, **_kwargs):
        return json.dumps(proposal)

    monkeypatch.setattr("app.multi_agent_api.complete", model)
    response = auth_client.post("/api/orchestrations/plan", json={
        "agent_id": agent["id"], "goal": "research",
        "allowed_tools": ["search"],
    })
    assert response.status_code == 201, response.text
    assert response.json()["workers"][0]["allowed_tools"] == ["search"]


def test_background_execution_is_observable_and_cancellable(auth_client, monkeypatch):
    agent = auth_client.post("/api/agents", json={"name": "Background Organizer"}).json()

    async def model(*_args, **_kwargs):
        await asyncio.sleep(2)
        return '{"answer":"late"}'

    monkeypatch.setattr("app.multi_agent_api.complete", model)
    planned = auth_client.post("/api/orchestrations/plan", json={
        "agent_id": agent["id"], "goal": "slow work", "max_workers": 1,
    }).json()
    started = auth_client.post(
        f"/api/orchestrations/{planned['run_id']}/execute?background=true"
    )
    assert started.status_code == 200
    assert auth_client.get(f"/api/orchestrations/{planned['run_id']}").json()["status"] == "running"
    cancelled = auth_client.post(f"/api/orchestrations/{planned['run_id']}/cancel")
    assert cancelled.status_code == 200
    assert auth_client.get(f"/api/orchestrations/{planned['run_id']}").json()["status"] == "cancelled"


def test_foreground_and_background_execute_both_receive_replan_handler(auth_client, monkeypatch):
    agent = auth_client.post("/api/agents", json={"name": "Replan Organizer"}).json()

    async def planner(*_args, **_kwargs):
        return ""  # deterministic plan, without executing an external model

    monkeypatch.setattr("app.multi_agent_api.complete", planner)
    first = auth_client.post("/api/orchestrations/plan", json={
        "agent_id": agent["id"], "goal": "foreground",
    }).json()
    second = auth_client.post("/api/orchestrations/plan", json={
        "agent_id": agent["id"], "goal": "background",
    }).json()
    handlers = {}
    background_called = threading.Event()

    async def capture_execute(self, scope, executor, *, replan=None, **_kwargs):
        handlers[scope.run_id] = replan
        if scope.run_id == second["run_id"]:
            background_called.set()
        return {"status": "succeeded", "results": []}

    monkeypatch.setattr("app.multi_agent_api.DurableOrchestrator.execute", capture_execute)
    foreground = auth_client.post(f"/api/orchestrations/{first['run_id']}/execute")
    background = auth_client.post(
        f"/api/orchestrations/{second['run_id']}/execute?background=true"
    )
    assert foreground.status_code == 200
    assert background.status_code == 200
    assert background_called.wait(1)
    assert handlers[first["run_id"]] is not None
    assert handlers[second["run_id"]] is not None
    assert handlers[first["run_id"]].__code__ is handlers[second["run_id"]].__code__


def test_dynamic_mcp_discovery_failure_is_stateful_audited_and_fail_closed(auth_client, monkeypatch):
    agent = auth_client.post("/api/agents", json={"name": "MCP Warning Organizer"}).json()
    with SessionLocal() as db:
        persisted = db.get(Agent, agent["id"])
        persisted.deploy_config_json = json.dumps({"allowed_connectors": ["github"]})
        db.commit()

    async def model(*_args, **_kwargs):
        return ""  # planning fallback; worker execution is still safe without MCP tools

    async def failed_specs():
        raise RuntimeError("secret-token-must-not-enter-audit")

    monkeypatch.setattr("app.multi_agent_api.complete", model)
    monkeypatch.setattr("app.multi_agent_runtime_adapter.github_mcp.is_configured", lambda: True)
    monkeypatch.setattr("app.multi_agent_runtime_adapter.github_mcp.tool_specs", failed_specs)
    planned = auth_client.post("/api/orchestrations/plan", json={
        "agent_id": agent["id"], "goal": "continue without unavailable dependency",
    }).json()
    executed = auth_client.post(f"/api/orchestrations/{planned['run_id']}/execute")
    assert executed.status_code == 200
    with SessionLocal() as db:
        run = db.get(OrchestrationRunRecord, planned["run_id"])
        warnings = run.scheduler_state["dependency_warnings"]
        assert warnings == [{
            "provider": "github",
            "category": "dependency",
            "error_type": "RuntimeError",
            "decision": "fail_closed",
        }]
        audit = db.scalar(select(GovernanceAuditRecord).where(
            GovernanceAuditRecord.run_id == planned["run_id"],
            GovernanceAuditRecord.action == "tool_registry.discovery",
        ))
        assert audit.decision == "dependency_warning"
        assert "secret-token" not in json.dumps(audit.details)
