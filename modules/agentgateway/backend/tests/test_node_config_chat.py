"""Backend tests for node_config_chat WS auto-fill (B1/B2/B4, D1/D7).

LLM calls are stubbed: ``create_agent`` returns a namespace carrying the system
prompt, and ``run_conversation`` is a fake async generator whose output is keyed
by the user turn. Extraction vs chat agents are told apart by their system
prompt (the extractor prompt contains "字段抽取器").
"""

from __future__ import annotations

import json
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, select, desc

from app.api import node_config_chat as ncc
from app.core.database import engine
from app.models.db import Agent, DAGGraph, NodeTypeRegistry

# node_config_chat captured `engine` at import; make sure it points at the
# conftest-patched test engine.
ncc.engine = engine

P_SCHEMA = json.dumps({
    "type": "object",
    "properties": {
        "role_name": {"type": "string"},
        "system_prompt": {"type": "string"},
        "output_format": {"type": "string"},
    },
})


# ── Fakes ─────────────────────────────────────────────────────────────────


class _FakeLLM:
    """Per-test response registry + create_agent call recorder."""

    def __init__(self) -> None:
        self.extraction: dict[str, str] = {}   # user_content -> raw JSON string
        self.chat: dict[str, str] = {}          # user_content -> assistant reply
        self.chat_prompts: list[str] = []       # recorded chat-agent system prompts

    def create_agent(self, system_prompt, model_name, provider="openai",
                      temperature=0.7, max_tokens=4096, **kwargs):
        is_extractor = "字段抽取器" in system_prompt
        if not is_extractor:
            self.chat_prompts.append(system_prompt)
        return types.SimpleNamespace(_system_prompt=system_prompt)

    def run_conversation(self, agent, user_content, attachments=None):
        sp = getattr(agent, "_system_prompt", "")
        is_extractor = "字段抽取器" in sp
        registry = self.extraction if is_extractor else self.chat

        async def _gen():
            payload = registry.get(user_content, "{}" if is_extractor else "")
            if payload:
                yield ("token", payload)
            yield ("done", "")

        return _gen()


@pytest.fixture
def fake_llm(monkeypatch) -> _FakeLLM:
    llm = _FakeLLM()
    monkeypatch.setattr(ncc, "create_agent", llm.create_agent)
    monkeypatch.setattr(ncc, "run_conversation", llm.run_conversation)
    return llm


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(ncc.router, prefix="/api")
    return TestClient(app)


def _seed(node_config: dict | None = None) -> int:
    with Session(engine) as session:
        agent = Agent(name="ncc-test")
        session.add(agent)
        session.commit()
        session.refresh(agent)
        graph = {
            "nodes": [
                {"id": "p1", "type": "p", "config": node_config or {}},
                {"id": "m1", "type": "m", "config": {"model_name": "x", "provider": "glm"}},
            ],
            "edges": [],
        }
        session.add(DAGGraph(
            agent_id=agent.id,
            graph_json=json.dumps(graph, ensure_ascii=False),
            state_schema="{}",
            version=1,
        ))
        # NodeTypeRegistry for "p" so config_schema resolves.
        if not session.exec(select(NodeTypeRegistry).where(NodeTypeRegistry.node_type == "p")).first():
            session.add(NodeTypeRegistry(
                node_type="p", display_name="提示词", category="core", config_schema=P_SCHEMA
            ))
        session.commit()
        return agent.id


def _latest_version(agent_id: int) -> int:
    with Session(engine) as session:
        dag = session.exec(
            select(DAGGraph).where(DAGGraph.agent_id == agent_id).order_by(desc(DAGGraph.version))
        ).first()
        return dag.version


def _latest_config(agent_id: int) -> dict:
    with Session(engine) as session:
        dag = session.exec(
            select(DAGGraph).where(DAGGraph.agent_id == agent_id).order_by(desc(DAGGraph.version))
        ).first()
        graph = json.loads(dag.graph_json)
        return next(n for n in graph["nodes"] if n["id"] == "p1")["config"]


# ── Scenarios ───────────────────────────────────────────────────────────────


def test_a_no_json_no_write(client, fake_llm):
    agent_id = _seed({"role_name": "旧"})
    fake_llm.chat = {"先聊聊": "我们先确认一下你需要的语气，没有任何 JSON。"}
    with client.websocket_connect(f"/api/node-config/ws/{agent_id}/p1") as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_json({"type": "message", "content": "先聊聊"})
        assert ws.receive_json()["type"] == "token"
        assert ws.receive_json()["type"] == "done"
    assert _latest_version(agent_id) == 1  # no new version written


def test_b_json_writes_and_commits(client, fake_llm):
    agent_id = _seed({"role_name": "旧"})
    fake_llm.chat = {
        "做个翻译助手": '建议如下：\n```json\n{"role_name": "翻译助手", "output_format": "text"}\n```'
    }
    with client.websocket_connect(f"/api/node-config/ws/{agent_id}/p1") as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_json({"type": "message", "content": "做个翻译助手"})
        assert ws.receive_json()["type"] == "token"
        assert ws.receive_json()["type"] == "done"
        committed = ws.receive_json()
    assert committed["type"] == "node_config_committed"
    assert committed["node_id"] == "p1"
    assert committed["version"] == 2
    assert set(committed["changed_keys"]) == {"role_name", "output_format"}
    cfg = _latest_config(agent_id)
    assert cfg["role_name"] == "翻译助手"
    assert "_chat_history" in cfg


def test_c_auto_commit_off_proposes_no_write(client, fake_llm):
    agent_id = _seed({"role_name": "旧"})
    fake_llm.chat = {
        "正式一点": '好的：\n```json\n{"role_name": "正式助手"}\n```'
    }
    with client.websocket_connect(f"/api/node-config/ws/{agent_id}/p1") as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_json({"type": "set_auto_commit", "value": False})
        ws.send_json({"type": "message", "content": "正式一点"})
        assert ws.receive_json()["type"] == "token"
        assert ws.receive_json()["type"] == "done"
        proposed = ws.receive_json()
    assert proposed["type"] == "node_config_proposed"
    assert proposed["partial"] == {"role_name": "正式助手"}
    assert proposed["changed_keys"] == ["role_name"]
    assert _latest_version(agent_id) == 1  # nothing written


def test_d_pending_config_accumulates_across_turns(client, fake_llm):
    agent_id = _seed({})
    fake_llm.extraction = {
        "角色名叫翻译助手": '{"role_name": "翻译助手"}',
        "风格正式一点": "{}",
    }
    fake_llm.chat = {"角色名叫翻译助手": "好的", "风格正式一点": "明白"}
    with client.websocket_connect(f"/api/node-config/ws/{agent_id}/p1") as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_json({"type": "message", "content": "角色名叫翻译助手"})
        assert ws.receive_json()["type"] == "token"
        assert ws.receive_json()["type"] == "done"
        ws.send_json({"type": "message", "content": "风格正式一点"})
        assert ws.receive_json()["type"] == "token"
        assert ws.receive_json()["type"] == "done"
    # Turn-2 chat prompt SHALL carry the pending field collected in turn 1.
    turn2_prompt = fake_llm.chat_prompts[-1]
    assert "已从前几轮对话收集到的字段" in turn2_prompt
    assert "翻译助手" in turn2_prompt


def test_e_reconnect_resets_pending(client, fake_llm):
    agent_id = _seed({})
    fake_llm.extraction = {"角色名叫翻译助手": '{"role_name": "翻译助手"}'}
    fake_llm.chat = {"角色名叫翻译助手": "好的", "你好": "在的"}

    with client.websocket_connect(f"/api/node-config/ws/{agent_id}/p1") as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_json({"type": "message", "content": "角色名叫翻译助手"})
        assert ws.receive_json()["type"] == "token"
        assert ws.receive_json()["type"] == "done"

    fake_llm.chat_prompts.clear()

    with client.websocket_connect(f"/api/node-config/ws/{agent_id}/p1") as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_json({"type": "message", "content": "你好"})
        assert ws.receive_json()["type"] == "token"
        assert ws.receive_json()["type"] == "done"
    # New connection started with empty pending_config → no pending block.
    first_prompt_new_conn = fake_llm.chat_prompts[0]
    assert "已从前几轮对话收集到的字段" not in first_prompt_new_conn
