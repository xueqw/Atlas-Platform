"""Tests for DAG projection guard: _derive_dag_from_agent_spec + apply empty-nodes rejection."""

import pytest


def test_derive_dag_from_agent_spec_generates_minimal_dag():
    """When nodes is empty but agent_spec has identity+runtime, derive P+M+Agent+O."""
    from app.api.planner.ws import _derive_dag_from_agent_spec

    structured = {
        "architecture_summary": "P+Agent+M",
        "nodes": [],
        "edges": [],
        "agent_spec": {
            "identity": {
                "role_name": "客服专家",
                "system_prompt": "你是一位专业的客服人员。",
                "output_format": "markdown",
            },
            "runtime": {
                "model_name": "glm-4.7-flash",
                "provider": "glm",
                "temperature": 0.5,
                "max_tokens": 2048,
                "streaming": True,
            },
        },
    }

    _derive_dag_from_agent_spec(structured)

    nodes = structured["nodes"]
    edges = structured["edges"]

    # 4 nodes: P, M, Agent, O
    assert len(nodes) == 4
    types = {n["id"]: n["type"] for n in nodes}
    assert types == {"p1": "p", "m1": "m", "agent1": "agent", "o1": "o"}

    # P node has correct config from identity
    p_node = next(n for n in nodes if n["id"] == "p1")
    assert p_node["config"]["role_name"] == "客服专家"
    assert p_node["config"]["system_prompt"] == "你是一位专业的客服人员。"

    # M node has correct config from runtime
    m_node = next(n for n in nodes if n["id"] == "m1")
    assert m_node["config"]["model_name"] == "glm-4.7-flash"
    assert m_node["config"]["provider"] == "glm"
    assert m_node["config"]["temperature"] == 0.5

    # Agent node merges both
    agent_node = next(n for n in nodes if n["id"] == "agent1")
    assert agent_node["config"]["role_name"] == "客服专家"
    assert agent_node["config"]["model_name"] == "glm-4.7-flash"

    # 3 edges
    assert len(edges) == 3
    edge_pairs = [(e["source"], e["target"]) for e in edges]
    assert ("p1", "agent1") in edge_pairs
    assert ("m1", "agent1") in edge_pairs
    assert ("agent1", "o1") in edge_pairs


def test_derive_dag_skips_when_nodes_already_present():
    """When nodes is non-empty, derivation is skipped."""
    from app.api.planner.ws import _derive_dag_from_agent_spec

    existing_nodes = [{"id": "x1", "type": "agent", "config": {}}]
    structured = {
        "nodes": existing_nodes,
        "edges": [],
        "agent_spec": {"identity": {"role_name": "X"}, "runtime": {"model_name": "m"}},
    }
    _derive_dag_from_agent_spec(structured)
    assert structured["nodes"] == existing_nodes  # unchanged


def test_derive_dag_skips_when_no_agent_spec():
    """When agent_spec is missing or empty, derivation is skipped."""
    from app.api.planner.ws import _derive_dag_from_agent_spec

    structured = {"nodes": [], "edges": []}
    _derive_dag_from_agent_spec(structured)
    assert structured["nodes"] == []

    structured2 = {"nodes": [], "edges": [], "agent_spec": {}}
    _derive_dag_from_agent_spec(structured2)
    assert structured2["nodes"] == []


def test_apply_proposal_rejects_empty_nodes(monkeypatch):
    """apply_proposal returns 400 when nodes list is empty."""
    from fastapi import HTTPException
    from app.api.planner.apply import apply_proposal

    class FakeBody:
        proposal = {"nodes": [], "edges": [], "architecture_summary": "test"}
        agent_name = "test"
        memory = {}
        conversation_id = None

    with pytest.raises(HTTPException) as exc_info:
        apply_proposal(FakeBody())
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["error"] == "empty_dag"
