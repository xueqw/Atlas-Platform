import json
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, HTTPException
from pydantic import BaseModel
from sqlmodel import Session

from app.core.database import engine
from app.models.db import Agent, PromptConfig, ModelConfig, DAGGraph
from app.models.schemas import AgentResponse, PromptConfigSchema, ModelConfigSchema, AgentStatusEnum

router = APIRouter(prefix="/builder", tags=["builder"])

# In-memory store for builder conversations (key: conv_id string, value: list of message dicts)
_builder_conversations: dict[str, list[dict]] = {}


class StartResponse(BaseModel):
    conversation_id: str
    greeting: str


class ApplyRequest(BaseModel):
    recommendation: dict  # {prompt: {...}, model: {...}, tools: [...]}
    agent_name: str = "新建智能体"


BUILDER_GREETING = (
    "你好！我是智能体构建向导。我可以帮你设计一个专属的智能体。\n\n"
    "请告诉我：你想构建一个什么类型的智能体？比如：\n"
    "- 客服助手\n"
    "- 技术支持\n"
    "- 代码审查\n"
    "- 数据分析\n"
    "- 其他..."
)


@router.post("/start", response_model=StartResponse)
def start_builder():
    import uuid
    conv_id = str(uuid.uuid4())
    _builder_conversations[conv_id] = []
    return StartResponse(conversation_id=conv_id, greeting=BUILDER_GREETING)


@router.websocket("/ws/{conversation_id}")
async def builder_websocket(websocket: WebSocket, conversation_id: str):
    await websocket.accept()

    if conversation_id not in _builder_conversations:
        _builder_conversations[conversation_id] = []

    from app.core.agentscope_runner import create_agent
    from app.core.builder_agent import run_builder_conversation, BUILDER_SYSTEM_PROMPT

    # Use GLM as default for builder agent (configurable via env)
    builder_agent = create_agent(
        system_prompt=BUILDER_SYSTEM_PROMPT,
        model_name="glm-4-flash",
        provider="glm",
        stream=False,
    )

    try:
        while True:
            data = await websocket.receive_text()
            msg = json.loads(data)

            if msg.get("type") == "message":
                user_content = msg.get("content", "")
                _builder_conversations[conversation_id].append({"role": "user", "content": user_content})

                full_response = ""
                try:
                    async for msg_type, content in run_builder_conversation(
                        builder_agent, user_content, _builder_conversations[conversation_id]
                    ):
                        if msg_type == "token":
                            full_response += content
                        await websocket.send_json({"type": msg_type, "content": content})
                except Exception as e:
                    await websocket.send_json({"type": "error", "message": str(e)})

                _builder_conversations[conversation_id].append({"role": "assistant", "content": full_response})
                await websocket.send_json({"type": "done"})

            elif msg.get("type") == "ping":
                await websocket.send_json({"type": "pong"})

    except WebSocketDisconnect:
        pass


@router.post("/apply")
def apply_recommendation(body: ApplyRequest):
    rec = body.recommendation

    with Session(engine) as session:
        # Create PromptConfig from recommendation
        prompt_data = rec.get("prompt", {})
        pc = PromptConfig(
            role_name=prompt_data.get("role_name", body.agent_name),
            role_description=prompt_data.get("role_description", ""),
            system_prompt=prompt_data.get("system_prompt", ""),
            output_format="markdown",
            constraints="[]",
        )
        session.add(pc)
        session.commit()
        session.refresh(pc)

        # Create ModelConfig from recommendation
        model_data = rec.get("model", {})
        mc = ModelConfig(
            provider=model_data.get("provider", "glm"),
            model_name=model_data.get("model_name", "glm-4-flash"),
            temperature=0.7,
            max_tokens=4096,
        )
        session.add(mc)
        session.commit()
        session.refresh(mc)

        agent = Agent(
            name=body.agent_name,
            description=rec.get("summary", ""),
            prompt_config_id=pc.id,
            model_config_id=mc.id,
        )
        session.add(agent)
        session.commit()
        session.refresh(agent)

        # Build DAG graph from recommendation or use default P→M
        dag_data = rec.get("dag")
        if dag_data and isinstance(dag_data, dict):
            dag_nodes = dag_data.get("nodes", [])
            dag_edges = dag_data.get("edges", [])
            from_rec = bool(dag_nodes)
        else:
            from_rec = False

        if from_rec:
            nodes = []
            auto_layout_spacing = 300
            for i, dn in enumerate(dag_nodes):
                node_type = dn.get("type", "p")
                node_id = f"{node_type}{i + 1}"
                pos = {"x": 100 + i * auto_layout_spacing, "y": 200}
                node_config = {}
                if node_type == "p":
                    node_config = {
                        "role_name": pc.role_name, "role_description": pc.role_description,
                        "system_prompt": pc.system_prompt, "output_format": pc.output_format,
                    }
                elif node_type == "m":
                    node_config = {
                        "model_name": mc.model_name, "provider": mc.provider,
                        "temperature": mc.temperature, "max_tokens": mc.max_tokens,
                        "streaming": mc.streaming,
                    }
                nodes.append({"id": node_id, "type": node_type, "position": pos, "config": node_config})
            edges = []
            for j, de in enumerate(dag_edges):
                from_idx = de.get("from") if isinstance(de.get("from"), int) else 0
                to_idx = de.get("to") if isinstance(de.get("to"), int) else 1
                if isinstance(from_idx, int) and isinstance(to_idx, int):
                    if 0 <= from_idx < len(dag_nodes) and 0 <= to_idx < len(dag_nodes):
                        from_type = dag_nodes[from_idx].get("type", "p")
                        to_type = dag_nodes[to_idx].get("type", "m")
                        edges.append({
                            "id": f"e{j + 1}",
                            "source": f"{from_type}{from_idx + 1}",
                            "target": f"{to_type}{to_idx + 1}",
                        })
            graph_json = json.dumps({"nodes": nodes, "edges": edges})
        else:
            graph_json = json.dumps({
                "nodes": [
                    {"id": "p1", "type": "p", "position": {"x": 100, "y": 200}, "config": {
                        "role_name": pc.role_name, "role_description": pc.role_description,
                        "system_prompt": pc.system_prompt, "output_format": pc.output_format,
                    }},
                    {"id": "m1", "type": "m", "position": {"x": 400, "y": 200}, "config": {
                        "model_name": mc.model_name, "provider": mc.provider,
                        "temperature": mc.temperature, "max_tokens": mc.max_tokens,
                        "streaming": mc.streaming,
                    }},
                ],
                "edges": [{"id": "e1", "source": "p1", "target": "m1"}],
            })

        dag = DAGGraph(agent_id=agent.id, graph_json=graph_json, state_schema="{}", version=1)
        session.add(dag)
        session.commit()

        prompt = PromptConfigSchema(
            role_name=pc.role_name,
            role_description=pc.role_description,
            output_format=pc.output_format,
            constraints=pc.constraints,
            system_prompt=pc.system_prompt,
        )
        model = ModelConfigSchema(
            provider=mc.provider,
            model_name=mc.model_name,
            temperature=mc.temperature,
            max_tokens=mc.max_tokens,
            top_p=mc.top_p,
            streaming=mc.streaming,
        )
        return AgentResponse(
            id=agent.id,
            name=agent.name,
            description=agent.description,
            status=AgentStatusEnum.DRAFT,
            version=agent.version,
            prompt_config=prompt,
            model=model,
            created_at=agent.created_at.isoformat(),
            updated_at=agent.updated_at.isoformat(),
        )
