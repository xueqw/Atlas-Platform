"""Legacy Pipeline → DAG Converter.

Converts old P-M-K-T linear pipeline agents to the new DAG format on first open.
"""

import json
from typing import Any, Dict


class PipelineToDAGConverter:
    """Convert legacy P→M→K→T pipeline config to P→M (+ optional K/T) DAG."""

    @staticmethod
    def convert(prompt_config: Dict[str, Any] | None,
                model_config: Dict[str, Any] | None,
                knowledge_config: Dict[str, Any] | None = None,
                tool_config: Dict[str, Any] | None = None) -> str:
        """Convert legacy pipeline configs to DAG graph JSON."""

        nodes = []
        edges = []
        node_idx = 0

        # P node
        if prompt_config:
            node_idx += 1
            nodes.append({
                "id": f"p{node_idx}",
                "type": "p",
                "position": {"x": 100, "y": 200},
                "config": {
                    "role_name": prompt_config.get("role_name", ""),
                    "role_description": prompt_config.get("role_description", ""),
                    "system_prompt": prompt_config.get("system_prompt", ""),
                    "output_format": prompt_config.get("output_format", "markdown"),
                },
            })
            p_id = f"p{node_idx}"

        # M node
        if model_config:
            node_idx += 1
            nodes.append({
                "id": f"m{node_idx}",
                "type": "m",
                "position": {"x": 400, "y": 200},
                "config": {
                    "model_name": model_config.get("model_name", "gpt-4o-mini"),
                    "provider": model_config.get("provider", "openai"),
                    "temperature": model_config.get("temperature", 0.7),
                    "max_tokens": model_config.get("max_tokens", 4096),
                    "streaming": model_config.get("streaming", False),
                },
            })
            m_id = f"m{node_idx}"

            if p_id:
                edges.append({"id": f"e_p_m", "source": p_id, "target": m_id})

        # Optional K node (positioned as branch from M)
        prev_id = m_id if model_config else (p_id if prompt_config else None)
        if knowledge_config and prev_id:
            node_idx += 1
            nodes.append({
                "id": f"k{node_idx}",
                "type": "k",
                "position": {"x": 700, "y": 100},
                "config": {
                    "collection_name": knowledge_config.get("collection_name", "default"),
                },
            })
            edges.append({"id": f"e_m_k", "source": prev_id, "target": f"k{node_idx}"})

        # Optional T node (positioned after K or M)
        if tool_config and prev_id:
            node_idx += 1
            nodes.append({
                "id": f"t{node_idx}",
                "type": "t",
                "position": {"x": 700, "y": 300},
                "config": {
                    "tool_name": tool_config.get("tool_name", ""),
                    "tool_params": tool_config.get("tool_params", "{}"),
                },
            })
            edges.append({"id": f"e_m_t", "source": prev_id, "target": f"t{node_idx}"})

        return json.dumps({"nodes": nodes, "edges": edges}, ensure_ascii=False)


def auto_convert_agent(agent, session) -> bool:
    """Check if a legacy agent needs conversion and convert if necessary.

    Returns True if conversion was performed.
    """
    from app.models.db import DAGGraph
    from sqlmodel import select

    # Check if agent already has a DAG graph
    existing = session.exec(
        select(DAGGraph).where(DAGGraph.agent_id == agent.id)
    ).first()
    if existing:
        return False

    # Legacy agent: has pipeline configs but no DAG
    prompt_config = None
    model_config = None

    if agent.prompt_config_id:
        from app.models.db import PromptConfig
        pc = session.get(PromptConfig, agent.prompt_config_id)
        if pc:
            prompt_config = {
                "role_name": pc.role_name,
                "role_description": pc.role_description,
                "system_prompt": pc.system_prompt,
                "output_format": pc.output_format,
            }

    if agent.model_config_id:
        from app.models.db import ModelConfig
        mc = session.get(ModelConfig, agent.model_config_id)
        if mc:
            model_config = {
                "model_name": mc.model_name,
                "provider": mc.provider,
                "temperature": mc.temperature,
                "max_tokens": mc.max_tokens,
                "streaming": mc.streaming,
            }

    graph_json = PipelineToDAGConverter.convert(
        prompt_config=prompt_config,
        model_config=model_config,
    )

    dag = DAGGraph(
        agent_id=agent.id,
        graph_json=graph_json,
        state_schema="{}",
        version=1,
    )
    session.add(dag)
    session.commit()
    return True
