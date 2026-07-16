import json
import asyncio
import time
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlmodel import Session, select, desc

from app.core.database import engine
from app.models.db import Agent, PromptConfig, ModelConfig, Message, Conversation, ToolConfig, DAGGraph

router = APIRouter()


async def _run_dag_chat(websocket: WebSocket, agent_id: int, user_content: str,
                        dag_graph: DAGGraph, conversation_id: int):
    """Execute DAG-based chat by delegating to DAGRunner.

    The runner drives execution (and Langfuse tracing + run-summary
    persistence); we translate its lifecycle events into the WebSocket message
    shape the frontend expects. Streaming token / thinking_content events are
    pushed straight through from the node's event hook.
    """
    from app.core.dag_executor import DAGParser, StateManager, DAGRunner

    parser = DAGParser(dag_graph.graph_json)
    if parser.has_cycles():
        await websocket.send_json({"type": "error", "message": "DAG 包含循环依赖，无法执行"})
        return "", True

    state = StateManager()
    runner = DAGRunner(parser, state_manager=state)

    has_error = False

    async def event_hook(evt: dict) -> None:
        etype = evt.get("type")
        if etype == "node_skipped":
            await websocket.send_json({
                "type": "node_status",
                "node_id": evt.get("node_id"),
                "node_type": evt.get("node_type", "unknown"),
                "status": "skipped",
            })
        elif etype == "node_error":
            nonlocal has_error
            has_error = True
            await websocket.send_json(evt)
        else:
            # node_start / node_complete / token / thinking_content pass through.
            await websocket.send_json(evt)

    result = await runner.run(
        user_content, agent_id, on_event=event_hook,
        dag_version=dag_graph.version or 0,
    )
    if result.error:
        has_error = True
        # A failure outside a node body (parse / topo-sort) never emits a
        # node_error, so the frontend would get no closing event and spin
        # forever. Emit an explicit error as a backstop (node-level failures
        # already sent their own node_error above; a second generic line is
        # harmless and keeps the contract simple).
        await websocket.send_json({"type": "error", "message": result.error})

    final_output = state.get_state().get("final_output") or result.final_output or ""
    return final_output, has_error


@router.websocket("/agent/{agent_id}")
async def websocket_agent_chat(websocket: WebSocket, agent_id: int):
    await websocket.accept()

    # Load agent config once
    with Session(engine) as session:
        agent = session.get(Agent, agent_id)
        if not agent:
            await websocket.send_json({"type": "error", "message": "智能体不存在"})
            await websocket.close()
            return

        # Check if this is a DAG-based agent
        dag_graph = session.exec(
            select(DAGGraph).where(DAGGraph.agent_id == agent_id).order_by(desc(DAGGraph.version))
        ).first()

        if dag_graph:
            # DAG-based agent — use DAG execution engine
            use_dag = True
            pc = mc = None
        else:
            # Legacy pipeline agent
            use_dag = False
            pc = session.get(PromptConfig, agent.prompt_config_id) if agent.prompt_config_id else None
            mc = session.get(ModelConfig, agent.model_config_id) if agent.model_config_id else None

            if not pc or not mc:
                await websocket.send_json({"type": "error", "message": "智能体配置不完整，请先配置 P 节点和 M 节点"})
                await websocket.close()
                return

            system_prompt = pc.system_prompt
            model_name = mc.model_name
            provider = mc.provider
            stream = mc.streaming
            temperature = mc.temperature
            max_tokens = mc.max_tokens

            # Load tool configs for this agent
            tool_rows = session.exec(select(ToolConfig).where(ToolConfig.agent_id == agent_id)).all()
            agent_tools = [
                {
                    "name": t.name,
                    "description": t.description,
                    "parameters": json.loads(t.parameters) if t.parameters else {},
                }
                for t in tool_rows
            ] if tool_rows else None

    conversation_id: int | None = None

    try:
        while True:
            data = await websocket.receive_text()
            msg = json.loads(data)

            if msg.get("type") == "message":
                user_content = msg.get("content", "")
                dag_mode = msg.get("dag_mode", False)

                # Resume an existing conversation when the client passes one
                # (history detail page reconnects to keep new turns in the same
                # thread). Only honour it once per socket and only if the row
                # exists and belongs to this agent — otherwise fall through to
                # creating a fresh conversation.
                if conversation_id is None:
                    requested_cid = msg.get("conversation_id")
                    if requested_cid is not None:
                        try:
                            requested_cid = int(requested_cid)
                        except (TypeError, ValueError):
                            requested_cid = None
                    if requested_cid is not None:
                        with Session(engine) as session:
                            existing = session.get(Conversation, requested_cid)
                            if existing and existing.agent_id == agent_id:
                                conversation_id = existing.id

                # Create conversation on first message if none resumed/exists
                if conversation_id is None:
                    with Session(engine) as session:
                        conv = Conversation(agent_id=agent_id, title=user_content[:50])
                        session.add(conv)
                        session.commit()
                        session.refresh(conv)
                        conversation_id = conv.id

                # Save user message
                with Session(engine) as session:
                    user_msg = Message(
                        conversation_id=conversation_id,
                        role="user",
                        content=user_content,
                    )
                    session.add(user_msg)
                    session.commit()

                if use_dag or dag_mode:
                    # Use DAG execution
                    if not dag_graph:
                        # Reload dag_graph for legacy agents with dag_mode
                        with Session(engine) as session:
                            dag_graph = session.exec(
                                select(DAGGraph).where(DAGGraph.agent_id == agent_id).order_by(desc(DAGGraph.version))
                            ).first()

                    if dag_graph:
                        full_response, has_error = await _run_dag_chat(
                            websocket, agent_id, user_content, dag_graph, conversation_id)
                        if not has_error:
                            await websocket.send_json({"type": "done"})
                    else:
                        await websocket.send_json({"type": "error", "message": "Agent has no DAG graph"})
                else:
                    # Legacy pipeline execution
                    from app.core.agentscope_runner import create_agent, run_conversation
                    from app.core import memory_service

                    override_system_prompt = msg.get("system_prompt", None)
                    effective_system_prompt = override_system_prompt or system_prompt

                    # Layered-memory injection (task 4.1). Policy-gated: when the
                    # agent has memory disabled, assemble returns an empty context
                    # and the rendered block is "", so the prompt is byte-identical
                    # to the pre-memory path. Best-effort — memory must never break
                    # the chat turn.
                    try:
                        mem_ctx = memory_service.assemble_layered_context(
                            memory_service.SCENARIO_AGENT_CHAT,
                            agent_id=agent_id,
                            conversation_id=conversation_id,
                            query=user_content,
                        )
                        mem_block = memory_service.render_layered_context_block(mem_ctx)
                        if mem_block:
                            effective_system_prompt = effective_system_prompt + mem_block
                    except Exception:
                        pass

                    agent_instance = create_agent(
                        system_prompt=effective_system_prompt,
                        model_name=model_name,
                        provider=provider,
                        stream=stream,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        tools=agent_tools,
                    )

                    full_response = ""
                    try:
                        async for msg_type, content in run_conversation(agent_instance, user_content):
                            if msg_type == "token":
                                full_response += content
                            await websocket.send_json({"type": msg_type, "content": content})
                    except Exception as e:
                        await websocket.send_json({"type": "error", "message": str(e)})

                    await websocket.send_json({"type": "done"})

                # Save assistant message
                if full_response:
                    with Session(engine) as session:
                        assistant_msg = Message(
                            conversation_id=conversation_id,
                            role="assistant",
                            content=full_response,
                        )
                        session.add(assistant_msg)
                        session.commit()

                    # Turn end: best-effort writeback enqueue (Batch D, task 5.1).
                    # Policy-gated inside the helper — no-op when memory is off, so
                    # the chat path is unchanged for memory-disabled agents.
                    from app.core import memory_service
                    memory_service.enqueue_writeback_if_enabled(
                        agent_id=agent_id, job_type="extract_profile",
                        source_kind="conversation", source_ref=str(conversation_id),
                        payload={"conversation_id": conversation_id, "agent_id": agent_id},
                    )

            elif msg.get("type") == "ping":
                await websocket.send_json({"type": "pong"})

    except WebSocketDisconnect:
        pass
