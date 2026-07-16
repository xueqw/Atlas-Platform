"""Apply / replan landing logic and the apply-related route handlers."""

import json
from typing import Dict, List, Optional
from fastapi import HTTPException
from sqlmodel import Session, select, desc
from app.core.database import engine
from app.core import redis_client
from app.models.db import (
    Agent,
    DAGGraph,
    ArchitectureProposal,
    PromptConfig,
    ModelConfig,
    PlannerSession,
    MEMORY_SCOPES,
    _utcnow,
)
from . import state
from .state import _REQUIRED_CONFIG_BY_TYPE
from .proposal import _parse_json_list
from .schemas import ApplyRequest, ApplyReplanRequest, PlannerSnapshotResponse
from app.core.model_caps import DEFAULT_CHAT_MODEL_ID, DEFAULT_CHAT_PROVIDER


def _validate_node_configs(nodes: List[dict]) -> List[Dict[str, object]]:
    """Return a list of {node_id, keys} for nodes missing required config keys.

    Empty list means the proposal is complete enough to apply."""
    missing: List[Dict[str, object]] = []
    for node in nodes:
        node_type = node.get("type")
        required = _REQUIRED_CONFIG_BY_TYPE.get(node_type)
        if not required:
            continue
        cfg = node.get("config") or {}
        missing_keys = [
            k for k in required
            if not isinstance(cfg.get(k), (int, float, bool)) and not (isinstance(cfg.get(k), str) and cfg.get(k).strip())
        ]
        if missing_keys:
            missing.append({"node_id": node.get("id", ""), "keys": missing_keys})
    return missing


def _pick_prompt_model_src(nodes: List[dict]) -> tuple:
    """Pick the canonical prompt/model config sources. Agent nodes carry both
    and are preferred; otherwise standalone p / m nodes supply them."""
    agent_node_cfg: Dict = {}
    p_node_cfg: Dict = {}
    m_node_cfg: Dict = {}
    for node in nodes:
        cfg = node.get("config") or {}
        ntype = node.get("type")
        if ntype == "agent" and not agent_node_cfg:
            agent_node_cfg = cfg
        elif ntype == "p" and not p_node_cfg:
            p_node_cfg = cfg
        elif ntype == "m" and not m_node_cfg:
            m_node_cfg = cfg
    return (agent_node_cfg or p_node_cfg or {}, agent_node_cfg or m_node_cfg or {})


def _create_prompt_model_rows(session: Session, prompt_src: dict, model_src: dict) -> tuple:
    """Create + flush PromptConfig / ModelConfig rows from node config sources,
    returning their ids. Caller owns the transaction."""
    raw_constraints = prompt_src.get("constraints")
    if isinstance(raw_constraints, list):
        constraints_json = json.dumps(raw_constraints, ensure_ascii=False)
    elif isinstance(raw_constraints, str) and raw_constraints.strip():
        constraints_json = raw_constraints
    else:
        constraints_json = "[]"

    allowed_output_formats = {"markdown", "text", "json"}
    raw_output_format = str(prompt_src.get("output_format", "markdown") or "markdown").lower()
    output_format = raw_output_format if raw_output_format in allowed_output_formats else "markdown"

    prompt_cfg_row = PromptConfig(
        role_name=str(prompt_src.get("role_name", "") or ""),
        role_description=str(prompt_src.get("role_description", "") or ""),
        output_format=output_format,
        constraints=constraints_json,
        system_prompt=str(prompt_src.get("system_prompt", "") or ""),
    )
    session.add(prompt_cfg_row)
    session.flush()

    model_cfg_row = ModelConfig(
        provider=str(model_src.get("provider", DEFAULT_CHAT_PROVIDER) or DEFAULT_CHAT_PROVIDER),
        model_name=str(model_src.get("model_name", DEFAULT_CHAT_MODEL_ID) or DEFAULT_CHAT_MODEL_ID),
        temperature=float(model_src.get("temperature", 0.7) or 0.7),
        max_tokens=int(model_src.get("max_tokens", 4096) or 4096),
        top_p=float(model_src.get("top_p", 1.0) or 1.0),
        streaming=bool(model_src.get("streaming", True)),
    )
    session.add(model_cfg_row)
    session.flush()
    return prompt_cfg_row.id, model_cfg_row.id


_MEMORY_PROVIDERS = {"none", "pgonly", "mem0"}


def _resolve_memory_policy(proposal: dict, memory: dict) -> Dict[str, object]:
    """Derive an Agent memory-policy patch from a proposal / apply body.

    Looks for a ``memory`` policy block in (priority order):
      1. ``body.memory["memory"]`` / ``body.memory["memory_policy"]``
      2. ``proposal["memory"]`` / ``proposal["agent_spec"]["memory"]``

    Missing or malformed → memory stays OFF with safe defaults, preserving the
    current (memory-less) behaviour for old proposals (spec: apply 落地 memory
    policy，旧 proposal 以「记忆默认关闭」安全缺省落地).

    Returns a dict of the Agent fields to set; never raises.
    """
    block = None
    for src in (memory.get("memory"), memory.get("memory_policy")):
        if isinstance(src, dict):
            block = src
            break
    if block is None:
        for src in (proposal.get("memory"), (proposal.get("agent_spec") or {}).get("memory")):
            if isinstance(src, dict):
                block = src
                break

    # Safe-default (memory off) patch. architecture_pattern is read from the
    # top-level proposal so a proposal carrying agent_spec / architecture_pattern
    # but no memory block still records the pattern (task 4.2 compat read).
    patch: Dict[str, object] = {
        "memory_enabled": False,
        "memory_provider": "none",
        "memory_scope": "",
        "memory_policy_json": "{}",
        "procedural_refs_json": "[]",
        "architecture_pattern": str(proposal.get("architecture_pattern", "") or ""),
    }
    if not isinstance(block, dict):
        return patch

    enabled = bool(block.get("enabled", False))
    provider = str(block.get("provider", "none") or "none").lower()
    if provider not in _MEMORY_PROVIDERS:
        provider = "none"
    # An enabled policy with provider=none is contradictory; treat as pgonly.
    if enabled and provider == "none":
        provider = "pgonly"
    scope = str(block.get("scope", "") or "")
    if scope and scope not in MEMORY_SCOPES:
        scope = ""

    refs = block.get("procedural_refs")
    refs_json = json.dumps(refs, ensure_ascii=False) if isinstance(refs, list) else "[]"

    patch.update({
        "memory_enabled": enabled,
        "memory_provider": provider if enabled else "none",
        "memory_scope": scope,
        "memory_policy_json": json.dumps(block, ensure_ascii=False),
        "procedural_refs_json": refs_json,
        "architecture_pattern": str(block.get("architecture_pattern", proposal.get("architecture_pattern", "")) or ""),
    })
    return patch


def _apply_memory_policy(agent: Agent, patch: Dict[str, object]) -> None:
    """Write a resolved memory-policy patch onto an Agent row in place."""
    for key, value in patch.items():
        setattr(agent, key, value)


def _has_memory_block(proposal: dict, memory: dict) -> bool:
    """True when a memory policy block is present in the body or proposal."""
    for src in (
        memory.get("memory"), memory.get("memory_policy"),
        proposal.get("memory"), (proposal.get("agent_spec") or {}).get("memory"),
    ):
        if isinstance(src, dict):
            return True
    return False


def _normalize_node_providers(nodes: List[dict], session: Session) -> None:
    """Overwrite each model-bearing node's ``provider`` with the authoritative
    one resolved from its ``model_name``.

    The planner LLM frequently mis-tags the provider (e.g. an OpenAI ``gpt-5.4``
    labelled ``glm``), which routes the model call to the wrong gateway and the
    agent silently returns nothing. We never trust the LLM's guess: the registry
    (then a prefix heuristic) decides. Mutates node configs in place so the
    correction flows into both the DAG graph_json and the ModelConfig row built
    from the same configs.
    """
    from app.core.model_caps import provider_for_model

    for node in nodes:
        cfg = node.get("config")
        if not isinstance(cfg, dict):
            continue
        model_name = cfg.get("model_name")
        if not model_name:
            continue
        cfg["provider"] = provider_for_model(str(model_name), session=session)


def _build_graph_json(nodes: List[dict], edges: List[dict]) -> str:
    """Serialize proposal nodes/edges into the canonical DAG graph_json shape,
    laying out positions deterministically.

    Planner proposals can omit edges even while emitting visible P/M/K/mem/tool
    capability nodes. Repair the minimal runtime wiring here so generated graphs
    are executable: input/capability nodes feed agent nodes, and agents feed
    output nodes.
    """
    dag_nodes = []
    node_ids = {node.get("id") for node in nodes if node.get("id")}
    node_types = {node.get("id"): node.get("type") for node in nodes if node.get("id")}

    for i, node in enumerate(nodes):
        dag_nodes.append({
            "id": node["id"],
            "type": node["type"],
            "position": {"x": 250 + (i % 3 - 1) * 200, "y": 40 + (i // 3) * 180},
            "config": node.get("config", {}),
        })

    repaired_edges = [dict(edge) for edge in edges if isinstance(edge, dict)]
    edge_pairs = {
        (edge.get("source"), edge.get("target"))
        for edge in repaired_edges
        if edge.get("source") in node_ids and edge.get("target") in node_ids
    }
    agent_ids = [nid for nid, ntype in node_types.items() if ntype == "agent"]
    input_node_ids = [
        nid for nid, ntype in node_types.items()
        if ntype in {"i", "p", "m", "k", "mem", "t"}
    ]
    output_node_ids = [nid for nid, ntype in node_types.items() if ntype == "o"]

    # If the proposal emits agent nodes but omits an output node, inject one
    # so the DAG is complete and DAGRunner can surface the final response.
    if agent_ids and not output_node_ids:
        o_id = "o1"
        suffix = 1
        while o_id in node_ids:
            suffix += 1
            o_id = f"o{suffix}"
        dag_nodes.append({
            "id": o_id,
            "type": "o",
            "position": {"x": 250, "y": 40 + (len(nodes) // 3 + 1) * 180},
            "config": {},
        })
        node_ids.add(o_id)
        node_types[o_id] = "o"
        output_node_ids.append(o_id)

    if agent_ids:
        # Feed every visible runtime capability into the first agent unless the
        # proposal already supplied an edge. This makes P/Agent/M/K/mem/T graphs
        # executable instead of parallel isolated boxes.
        first_agent = agent_ids[0]
        for source in input_node_ids:
            if source != first_agent and (source, first_agent) not in edge_pairs:
                repaired_edges.append({"source": source, "target": first_agent})
                edge_pairs.add((source, first_agent))
        for agent_id in agent_ids:
            for target in output_node_ids:
                if agent_id != target and (agent_id, target) not in edge_pairs:
                    repaired_edges.append({"source": agent_id, "target": target})
                    edge_pairs.add((agent_id, target))

    dag_edges = []
    for i, edge in enumerate(repaired_edges):
        source = edge.get("source")
        target = edge.get("target")
        if source not in node_ids or target not in node_ids:
            continue
        dag_edges.append({
            "id": edge.get("id") or f"e{i+1}",
            "source": source,
            "target": target,
            "sourceHandle": edge.get("sourceHandle"),
            "targetHandle": edge.get("targetHandle"),
        })
    return json.dumps({"nodes": dag_nodes, "edges": dag_edges}, ensure_ascii=False)


def _merge_partial_graph(existing_graph_json: str, proposal: dict, selected_node_ids: List[str]) -> dict:
    """Merge only the confirmed node subset of a Replan proposal into the
    existing graph (scenario: 局部应用只改确认项).

    - Confirmed nodes present in the proposal are added/updated.
    - Confirmed nodes flagged as removed (in diff.removed) are dropped.
    - All other existing nodes/edges are kept untouched.
    Returns a dict {nodes, edges}."""
    try:
        existing = json.loads(existing_graph_json or "{}")
    except (json.JSONDecodeError, TypeError):
        existing = {}
    existing_nodes = {n.get("id"): n for n in (existing.get("nodes") or []) if isinstance(n, dict)}
    existing_edges = list(existing.get("edges") or [])

    selected = set(selected_node_ids or [])
    diff = proposal.get("diff") or {}
    removed = set(diff.get("removed") or [])
    proposal_nodes = {n.get("id"): n for n in (proposal.get("nodes") or []) if isinstance(n, dict)}

    # Apply confirmed additions/updates.
    for nid in selected:
        if nid in removed:
            existing_nodes.pop(nid, None)
        elif nid in proposal_nodes:
            existing_nodes[nid] = proposal_nodes[nid]
    # Confirmed removals (even if not in selected explicitly).
    for nid in (selected & removed):
        existing_nodes.pop(nid, None)

    merged_node_ids = set(existing_nodes.keys())
    # Bring in proposal edges that touch only nodes still present; keep existing
    # edges whose endpoints survive. Dedup by (source,target,targetHandle).
    def _edge_key(e: dict) -> tuple:
        return (e.get("source"), e.get("target"), e.get("targetHandle"))
    kept_edges = [e for e in existing_edges if isinstance(e, dict)
                  and e.get("source") in merged_node_ids and e.get("target") in merged_node_ids]
    seen = {_edge_key(e) for e in kept_edges}
    for e in (proposal.get("edges") or []):
        if not isinstance(e, dict):
            continue
        if e.get("source") in merged_node_ids and e.get("target") in merged_node_ids:
            if _edge_key(e) not in seen:
                kept_edges.append(e)
                seen.add(_edge_key(e))
    return {"nodes": list(existing_nodes.values()), "edges": kept_edges}


def apply_proposal(body: ApplyRequest):
    proposal = body.proposal
    nodes = proposal.get("nodes", [])
    edges = proposal.get("edges", [])
    memory = body.memory

    # DAG projection guard: refuse to create an agent with zero nodes.
    if not nodes:
        raise HTTPException(
            status_code=400,
            detail={"error": "empty_dag", "message": "方案无 DAG 节点，无法创建智能体"},
        )

    # Validate required config keys per node type before touching the DB.
    missing = _validate_node_configs(nodes)
    if missing:
        raise HTTPException(
            status_code=400,
            detail={"error": "incomplete_proposal", "missing": missing},
        )

    prompt_src, _ = _pick_prompt_model_src(nodes)

    with Session(engine) as session:
        try:
            # Correct mis-tagged providers (registry-authoritative) before any
            # config is persisted, then (re)pick the model source so the
            # ModelConfig row and DAG node configs agree on provider.
            _normalize_node_providers(nodes, session)
            _, model_src = _pick_prompt_model_src(nodes)
            prompt_cfg_id, model_cfg_id = _create_prompt_model_rows(session, prompt_src, model_src)

            agent = Agent(
                name=body.agent_name,
                description=proposal.get("architecture_summary", ""),
                prompt_config_id=prompt_cfg_id,
                model_config_id=model_cfg_id,
            )
            # Land planner memory policy (off by default for old proposals).
            _apply_memory_policy(agent, _resolve_memory_policy(proposal, memory))
            session.add(agent)
            session.flush()

            graph_json = _build_graph_json(nodes, edges)
            dag = DAGGraph(agent_id=agent.id, graph_json=graph_json, state_schema="{}", version=1)
            session.add(dag)
            session.flush()

            # Ensure the planner-created DAG becomes the canonical latest graph.
            # Older historical rows may already exist for the same agent id in dev DBs,
            # so writing via the normal save endpoint semantics avoids falling back to
            # a stale higher-version graph in /dag-graph reads.
            existing_dags = session.exec(
                select(DAGGraph).where(DAGGraph.agent_id == agent.id).order_by(desc(DAGGraph.version))
            ).all()
            if existing_dags:
                latest_version = max((row.version or 0) for row in existing_dags)
                if dag.version <= latest_version:
                    dag.version = latest_version + 1
                    session.add(dag)
                    session.flush()

            # Save proposal with planning metadata
            prop = ArchitectureProposal(
                agent_id=agent.id,
                trigger_type="create",
                user_request=body.agent_name,
                requirement_summary=memory.get("requirement_summary", proposal.get("architecture_summary", "")),
                proposed_graph_json=graph_json,
                rationale=proposal.get("rationale", ""),
                status="applied",
                confirmed_constraints=json.dumps(memory.get("confirmed_constraints", []), ensure_ascii=False),
                task_classification=memory.get("task_classification", ""),
                proposal_version=1,
                user_feedback_summary=json.dumps(memory.get("user_feedback", []), ensure_ascii=False),
            )
            session.add(prop)

            # Link the planner session row (if any) to the new agent so /sessions list
            # shows "applied" status and the from-agent reverse lookup can find it.
            if body.conversation_id:
                ps_row = session.exec(
                    select(PlannerSession).where(PlannerSession.conversation_id == body.conversation_id)
                ).first()
                if ps_row is not None:
                    ps_row.linked_agent_id = agent.id
                    ps_row.stage = "applied"
                    ps_row.last_updated_at = _utcnow()
                    session.add(ps_row)

            session.commit()
            session.refresh(agent)

            # Mirror to working-state cache so the WebSocket sees the latest
            # state on next turn (store_conv flushes on the Redis backend).
            if body.conversation_id:
                cd = state.load_conv(body.conversation_id)
                if cd is not None:
                    cd["linked_agent_id"] = agent.id
                    cd["stage"] = "applied"
                    state.store_conv(body.conversation_id, cd)
        except Exception:
            session.rollback()
            raise

        # Proposal landed: best-effort episodic writeback (Batch D, task 5.1).
        # Policy-gated inside the helper — no-op when memory is off.
        from app.core import memory_service
        memory_service.enqueue_writeback_if_enabled(
            agent_id=agent.id, job_type="extract_episodic",
            source_kind="proposal", source_ref=f"agent:{agent.id}",
            payload={"agent_id": agent.id, "source_kind": "proposal",
                     "summary": (proposal.get("rationale") or proposal.get("requirement_summary") or "")},
        )

        return {"id": agent.id, "name": agent.name}


def apply_replan(body: ApplyReplanRequest):
    """Land a Replan proposal onto an existing agent in one of three modes.

    All writes happen inside a single transaction; any failure rolls back so no
    half-written DAG/config rows survive (spec: 落地失败回滚).

    Idempotency (Batch C): concurrent applies for the same
    ``(agent_id, conversation_id)`` are serialised by a TTL lock — only one runs,
    the rest get HTTP 409「处理中」rather than each landing a duplicate version.
    """
    proposal = body.proposal
    nodes = proposal.get("nodes", []) or []
    edges = proposal.get("edges", []) or []
    memory = body.memory
    landing = body.landing or "save_new_version"
    if landing not in {"save_new_version", "override_draft", "partial"}:
        raise HTTPException(status_code=400, detail={"error": "invalid_landing", "landing": landing})

    lock_name = f"replan:{body.agent_id}:{body.conversation_id or '-'}"
    lock_token = redis_client.acquire_lock(lock_name, ttl=60)
    if lock_token is None:
        raise HTTPException(status_code=409, detail={"error": "replan_in_progress", "message": "该方案正在落地中，请稍候"})

    try:
        return _apply_replan_locked(body, proposal, nodes, edges, memory, landing)
    finally:
        redis_client.release_lock(lock_name, lock_token)


def _apply_replan_locked(body, proposal, nodes, edges, memory, landing):
    with Session(engine) as session:
        agent = session.get(Agent, body.agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail="agent not found")

        existing_dag = session.exec(
            select(DAGGraph).where(DAGGraph.agent_id == body.agent_id).order_by(desc(DAGGraph.version))
        ).first()

        # Build the target graph for this landing mode.
        if landing == "partial":
            if existing_dag is None:
                raise HTTPException(
                    status_code=400,
                    detail={"error": "no_existing_graph", "message": "partial apply requires an existing graph"},
                )
            merged = _merge_partial_graph(existing_dag.graph_json, proposal, body.selected_node_ids)
            target_nodes = merged["nodes"]
            target_edges = merged["edges"]
        else:
            target_nodes = nodes
            target_edges = edges

        # Validate the final node set before touching the DB.
        missing = _validate_node_configs(target_nodes)
        if missing:
            raise HTTPException(
                status_code=400,
                detail={"error": "incomplete_proposal", "missing": missing},
            )

        # Correct mis-tagged providers (registry-authoritative) before the
        # ModelConfig row and DAG graph_json are built from these configs.
        _normalize_node_providers(target_nodes, session)
        prompt_src, model_src = _pick_prompt_model_src(target_nodes)
        graph_json = _build_graph_json(target_nodes, target_edges)

        try:
            # Refresh the agent's prompt/model config to the revised canonical source.
            prompt_cfg_id, model_cfg_id = _create_prompt_model_rows(session, prompt_src, model_src)
            agent.prompt_config_id = prompt_cfg_id
            agent.model_config_id = model_cfg_id
            if proposal.get("architecture_summary"):
                agent.description = proposal.get("architecture_summary", "")
            # Re-land memory policy only when the replan proposal actually carries
            # one; otherwise preserve the agent's existing policy untouched.
            if _has_memory_block(proposal, memory):
                _apply_memory_policy(agent, _resolve_memory_policy(proposal, memory))
            agent.updated_at = _utcnow()
            session.add(agent)

            latest_version = (existing_dag.version or 0) if existing_dag else 0
            if landing == "override_draft" and existing_dag is not None:
                # Override the current latest version in place — old graph row is
                # replaced, not preserved.
                existing_dag.graph_json = graph_json
                existing_dag.updated_at = _utcnow()
                session.add(existing_dag)
                new_version = existing_dag.version
            else:
                # save_new_version / partial: write v(N+1) as the new latest, keeping
                # the prior version(s) accessible.
                new_version = latest_version + 1
                dag = DAGGraph(
                    agent_id=body.agent_id,
                    graph_json=graph_json,
                    state_schema=(existing_dag.state_schema if existing_dag else "{}"),
                    version=new_version,
                )
                session.add(dag)
            session.flush()

            prop = ArchitectureProposal(
                agent_id=body.agent_id,
                trigger_type="replan",
                user_request=agent.name or "",
                requirement_summary=memory.get("requirement_summary", proposal.get("architecture_summary", "")),
                proposed_graph_json=graph_json,
                rationale=proposal.get("rationale", ""),
                status="applied",
                confirmed_constraints=json.dumps(memory.get("confirmed_constraints", []), ensure_ascii=False),
                task_classification=memory.get("task_classification", ""),
                proposal_version=new_version,
                user_feedback_summary=json.dumps(memory.get("user_feedback", []), ensure_ascii=False),
            )
            session.add(prop)

            if body.conversation_id:
                ps_row = session.exec(
                    select(PlannerSession).where(PlannerSession.conversation_id == body.conversation_id)
                ).first()
                if ps_row is not None:
                    ps_row.linked_agent_id = body.agent_id
                    ps_row.stage = "applied"
                    ps_row.last_updated_at = _utcnow()
                    session.add(ps_row)

            session.commit()
            session.refresh(agent)

            if body.conversation_id:
                cd = state.load_conv(body.conversation_id)
                if cd is not None:
                    cd["linked_agent_id"] = body.agent_id
                    cd["stage"] = "applied"
                    state.store_conv(body.conversation_id, cd)
        except HTTPException:
            session.rollback()
            raise
        except Exception:
            session.rollback()
            raise

        return {"id": agent.id, "name": agent.name, "version": new_version, "landing": landing}


def get_planner_snapshot(agent_id: int):
    with Session(engine) as session:
        applied = session.exec(
            select(ArchitectureProposal)
            .where(ArchitectureProposal.agent_id == agent_id, ArchitectureProposal.status == "applied")
            .order_by(ArchitectureProposal.created_at.desc())
        ).first()
        latest = applied or session.exec(
            select(ArchitectureProposal)
            .where(ArchitectureProposal.agent_id == agent_id)
            .order_by(ArchitectureProposal.created_at.desc())
        ).first()
        if latest is None:
            raise HTTPException(status_code=404, detail="no proposal")
        return PlannerSnapshotResponse(
            requirement_summary=latest.requirement_summary or "",
            task_classification=latest.task_classification or "",
            confirmed_constraints=_parse_json_list(latest.confirmed_constraints),
            rationale=latest.rationale or "",
            user_feedback_summary=_parse_json_list(latest.user_feedback_summary),
            proposal_version=latest.proposal_version or 1,
            created_at=latest.created_at,
        )
