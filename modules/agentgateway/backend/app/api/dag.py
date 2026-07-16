"""DAG Graph Storage API — CRUD for DAG graph JSON with versioning and validation."""

import json
import time
from datetime import datetime
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlmodel import Session, select, desc

from app.core.database import get_session
from app.core.dag_executor import DAGParser
from app.core.hermes import ParameterValidator, ConnectionConstraintEngine, ValidationReport
from app.core.nodes.base import NodeRegistry
from app.models.db import DAGGraph, Agent
from app.models.schemas import DAGGraphCreate, DAGGraphUpdate, DAGGraphResponse, DAGValidateResponse

router = APIRouter(prefix="/agents", tags=["dag"])


class DebugNodeRequest(BaseModel):
    node_id: str
    node_type: str
    config: dict = {}
    inputs: dict = {}


@router.post("/{agent_id}/debug-node")
async def debug_node(agent_id: int, payload: DebugNodeRequest):
    """Run a single node in isolation for debugging purposes."""
    node_cls = NodeRegistry.get(payload.node_type)
    if not node_cls:
        raise HTTPException(status_code=404, detail=f"Unknown node type: {payload.node_type}")

    try:
        instance = node_cls(config=payload.config)
        t_start = time.time()
        output = await instance.run(payload.inputs, {})
        elapsed_ms = round((time.time() - t_start) * 1000)
        return {
            "node_id": payload.node_id,
            "node_type": payload.node_type,
            "output": output,
            "duration_ms": elapsed_ms,
            "tokens": 0,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


def _dag_response(dag: DAGGraph) -> DAGGraphResponse:
    return DAGGraphResponse(
        id=dag.id,
        agent_id=dag.agent_id,
        graph_json=dag.graph_json,
        state_schema=dag.state_schema,
        version=dag.version,
        created_at=dag.created_at.isoformat(),
        updated_at=dag.updated_at.isoformat(),
    )


def _normalize_graph_json_providers(graph_json: str, session: Session) -> str:
    """Rewrite each model-bearing node's ``provider`` to the authoritative value
    resolved from its ``model_name`` before the graph is persisted.

    Reuses the apply pipeline's ``_normalize_node_providers`` so every write path
    (manual save here, node-chat self-fill, planner apply) lands the same
    authoritative provider — a model switch in the editor can't leave a stale
    provider that routes execution to the wrong gateway. Idempotent: nodes whose
    provider already matches are unchanged. On parse failure the input is
    returned untouched (validation elsewhere surfaces malformed graphs).
    """
    from app.api.planner.apply import _normalize_node_providers

    try:
        graph = json.loads(graph_json or "{}")
    except (json.JSONDecodeError, TypeError):
        return graph_json
    nodes = graph.get("nodes")
    if not isinstance(nodes, list):
        return graph_json
    _normalize_node_providers(nodes, session)
    return json.dumps(graph, ensure_ascii=False)


@router.post("/{agent_id}/dag-graph", response_model=DAGGraphResponse)
def save_dag_graph(agent_id: int, payload: DAGGraphCreate, session: Session = Depends(get_session)):
    agent = session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    graph_json = _normalize_graph_json_providers(payload.graph_json, session)

    existing = session.exec(
        select(DAGGraph).where(DAGGraph.agent_id == agent_id).order_by(desc(DAGGraph.version))
    ).first()

    if existing:
        new_version = existing.version + 1
        dag = DAGGraph(
            agent_id=agent_id,
            graph_json=graph_json,
            state_schema=payload.state_schema,
            version=new_version,
        )
    else:
        dag = DAGGraph(
            agent_id=agent_id,
            graph_json=graph_json,
            state_schema=payload.state_schema,
            version=1,
        )

    session.add(dag)
    session.commit()
    session.refresh(dag)
    return _dag_response(dag)


@router.get("/{agent_id}/dag-graph", response_model=DAGGraphResponse)
def get_dag_graph(agent_id: int, session: Session = Depends(get_session)):
    dag = session.exec(
        select(DAGGraph).where(DAGGraph.agent_id == agent_id).order_by(desc(DAGGraph.version))
    ).first()
    if not dag:
        raise HTTPException(status_code=404, detail="DAG graph not found")
    return _dag_response(dag)


@router.get("/{agent_id}/dag-graph/versions", response_model=DAGGraphResponse)
def get_dag_graph_version(agent_id: int, version: int = Query(...), session: Session = Depends(get_session)):
    dag = session.exec(
        select(DAGGraph).where(DAGGraph.agent_id == agent_id, DAGGraph.version == version)
    ).first()
    if not dag:
        raise HTTPException(status_code=404, detail="DAG graph version not found")
    return _dag_response(dag)


@router.get("/{agent_id}/dag-graph/versions/list", response_model=List[int])
def list_dag_versions(agent_id: int, session: Session = Depends(get_session)):
    versions = session.exec(
        select(DAGGraph.version).where(DAGGraph.agent_id == agent_id).order_by(desc(DAGGraph.version))
    ).all()
    return versions


@router.post("/{agent_id}/dag-graph/validate", response_model=DAGValidateResponse)
def validate_dag_graph(agent_id: int, payload: DAGGraphCreate):
    """Validate a DAG graph: parameter validation + connection constraints. Does not persist."""
    try:
        parser = DAGParser(payload.graph_json)
    except Exception as exc:
        return DAGValidateResponse(
            valid=False,
            errors=[{"node_id": "*", "node_type": "*", "rule_type": "connection_constraint",
                      "severity": "error", "message": f"Invalid graph JSON: {str(exc)}"}],
            warnings=[],
        )

    param_report = ParameterValidator.validate_dag(parser)
    conn_report = ConnectionConstraintEngine.evaluate(parser)

    errors = []
    warnings = []

    for err in param_report.errors:
        errors.append({"node_id": err.node_id, "node_type": err.node_type,
                        "rule_type": err.rule_type, "severity": err.severity, "message": err.message})
    for w in param_report.warnings:
        warnings.append({"node_id": w.node_id, "node_type": w.node_type,
                          "rule_type": w.rule_type, "severity": w.severity, "message": w.message})
    for err in conn_report.errors:
        errors.append({"node_id": err.node_id, "node_type": err.node_type,
                        "rule_type": err.rule_type, "severity": err.severity, "message": err.message})
    for w in conn_report.warnings:
        warnings.append({"node_id": w.node_id, "node_type": w.node_type,
                          "rule_type": w.rule_type, "severity": w.severity, "message": w.message})

    return DAGValidateResponse(
        valid=len([e for e in errors if e["severity"] == "error"]) == 0,
        errors=errors,
        warnings=warnings,
    )
