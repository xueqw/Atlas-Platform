"""Unit tests for dag_node_config_merger (D2/D3/D4/D5/D6)."""

from __future__ import annotations

import json

import pytest
from sqlmodel import Session, select, desc

from app.core import dag_node_config_merger as m
from app.core.database import engine
from app.models.db import Agent, DAGGraph


SCHEMA = json.dumps({
    "type": "object",
    "properties": {
        "role_name": {"type": "string"},
        "system_prompt": {"type": "string"},
        "temperature": {"type": "number"},
        "streaming": {"type": "boolean"},
    },
})


def _seed_agent_with_node(node_config: dict | None = None) -> int:
    """Create an agent + one DAGGraph (version 1) with a single 'p1' node."""
    with Session(engine) as session:
        agent = Agent(name="merger-test")
        session.add(agent)
        session.commit()
        session.refresh(agent)
        graph = {
            "nodes": [{"id": "p1", "type": "p", "config": node_config or {}}],
            "edges": [],
        }
        dag = DAGGraph(
            agent_id=agent.id,
            graph_json=json.dumps(graph, ensure_ascii=False),
            state_schema="{}",
            version=1,
        )
        session.add(dag)
        session.commit()
        return agent.id


def _latest_node_config(agent_id: int) -> dict:
    with Session(engine) as session:
        dag = session.exec(
            select(DAGGraph).where(DAGGraph.agent_id == agent_id).order_by(desc(DAGGraph.version))
        ).first()
        graph = json.loads(dag.graph_json)
        return next(n for n in graph["nodes"] if n["id"] == "p1")["config"]


# ── D2: extract_config_block ──────────────────────────────────────────────


def test_extract_fenced_json_block():
    text = '解释一下\n```json\n{"role_name": "翻译助手"}\n```\n收尾'
    assert m.extract_config_block(text) == {"role_name": "翻译助手"}


def test_extract_no_block_returns_none():
    assert m.extract_config_block("这是一段没有任何 JSON 的散文。") is None


def test_extract_fallback_balanced_object():
    text = '没有围栏但有对象 {"role_name": "X", "nested": {"a": 1}} 后面'
    assert m.extract_config_block(text) == {"role_name": "X", "nested": {"a": 1}}


# ── D3: validate_against_schema ───────────────────────────────────────────


def test_validate_drops_unrecognized_keys():
    filtered, warnings = m.validate_against_schema(
        {"role_name": "X", "priority": 5}, SCHEMA
    )
    assert filtered == {"role_name": "X"}
    assert warnings == []


def test_validate_type_mismatch_warns_but_keeps():
    filtered, warnings = m.validate_against_schema(
        {"temperature": "0.7"}, SCHEMA
    )
    assert filtered == {"temperature": "0.7"}  # kept despite wrong type
    assert len(warnings) == 1
    assert "temperature" in warnings[0]


# ── D4/D5/D6: merge_into_graph ────────────────────────────────────────────


def test_merge_partial_replace_preserves_other_fields():
    agent_id = _seed_agent_with_node({"role_name": "旧名", "system_prompt": "保留我"})
    version, changed = m.merge_into_graph(agent_id, "p1", {"role_name": "新名"})
    cfg = _latest_node_config(agent_id)
    assert cfg["role_name"] == "新名"
    assert cfg["system_prompt"] == "保留我"  # untouched
    assert changed == ["role_name"]


def test_merge_bumps_version():
    agent_id = _seed_agent_with_node({"role_name": "X"})
    version, _ = m.merge_into_graph(agent_id, "p1", {"role_name": "Y"})
    assert version == 2  # seeded at version 1


def test_merge_creates_chat_history_first_time():
    agent_id = _seed_agent_with_node({})
    m.merge_into_graph(agent_id, "p1", {"role_name": "首次"})
    cfg = _latest_node_config(agent_id)
    history = cfg[m.CHAT_HISTORY_KEY]
    assert len(history) == 1
    assert history[0]["turn"] == 1
    assert history[0]["changed_keys"] == ["role_name"]
    assert history[0]["source"] == "assistant_extracted"
    assert "at" in history[0]


def test_merge_chat_history_fifo_caps_at_20():
    agent_id = _seed_agent_with_node({})
    for i in range(21):
        m.merge_into_graph(agent_id, "p1", {"role_name": f"v{i}"})
    cfg = _latest_node_config(agent_id)
    history = cfg[m.CHAT_HISTORY_KEY]
    assert len(history) == 20
    # earliest turn (1) evicted; oldest remaining is turn 2
    assert history[0]["turn"] == 2
    assert history[-1]["turn"] == 21
