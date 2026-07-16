"""WebSocket endpoint for AI-assisted node configuration via chat."""

import json
import sys
from fastapi import APIRouter, WebSocket
from sqlmodel import Session, select, desc

from app.core.database import engine
from app.models.db import DAGGraph, NodeTypeRegistry
from app.core.agentscope_runner import create_agent, run_conversation
from app.core import dag_node_config_merger as merger

router = APIRouter()


def _build_node_config_prompt(
    node_type: str,
    config_schema: str,
    current_config: dict,
    pending_config: dict | None = None,
) -> str:
    pending_block = ""
    if pending_config:
        pending_block = f"""

目前已从前几轮对话收集到的字段（请在生成完整配置时一并包含，不要重复询问）：
```json
{json.dumps(pending_config, ensure_ascii=False, indent=2)}
```"""

    return f"""你是一个 DAG 节点配置助手。用户正在配置一个类型为 "{node_type}" 的节点。

该节点的配置 schema 如下：
```json
{config_schema}
```

当前配置：
```json
{json.dumps(current_config, ensure_ascii=False, indent=2)}
```{pending_block}

你的任务：
1. 理解用户用自然语言描述的需求
2. 根据需求生成合适的节点配置
3. 先简要解释你的配置建议（1-2句话）
4. 然后输出一个 JSON 代码块，包含建议的完整配置

输出格式示例：
根据你的需求，我建议如下配置：

```json
{{"key": "value"}}
```

注意：
- 只输出 config_schema 中定义的字段
- 保持 JSON 格式正确
- 如果用户的需求不明确，先询问再生成配置"""


def _build_extraction_prompt(config_schema: str) -> str:
    return f"""你是一个字段抽取器。下面是一个节点的配置 schema：
```json
{config_schema}
```

用户会发来一句话。你的任务：把用户这句话里**明确提到**的、且属于上面 schema 的字段，抽成一个 JSON 字典。

严格要求：
- 只输出一个 JSON 对象，不要任何解释、不要 markdown 代码块标记。
- 只包含 schema 里有的 key；用户没提到的字段不要编。
- 如果这句话没提到任何 schema 字段，输出 `{{}}`。"""


async def _extract_pending_fields(
    extraction_agent, user_content: str, config_schema: str
) -> dict:
    """Run a lightweight LLM pass to pull schema fields out of the user turn.

    Returns the recognized-and-filtered dict, or {} on any failure (silent).
    """
    try:
        full = ""
        async for event_type, content in run_conversation(extraction_agent, user_content):
            if event_type == "token":
                full += content
        parsed = merger.extract_config_block(full)
        if not parsed:
            return {}
        filtered, _ = merger.validate_against_schema(parsed, config_schema)
        return filtered
    except Exception as exc:  # noqa: BLE001 — extraction is best-effort
        print(f"[node_config_chat] pending extraction failed: {exc}", file=sys.stderr)
        return {}


@router.websocket("/node-config/ws/{agent_id}/{node_id}")
async def node_config_chat(websocket: WebSocket, agent_id: int, node_id: str):
    await websocket.accept()

    with Session(engine) as session:
        dag_graph = session.exec(
            select(DAGGraph).where(DAGGraph.agent_id == agent_id).order_by(desc(DAGGraph.version))
        ).first()

        if not dag_graph:
            await websocket.send_json({"type": "error", "message": "未找到 DAG 图"})
            await websocket.close()
            return

        graph = json.loads(dag_graph.graph_json)
        node_data = next((n for n in graph.get("nodes", []) if n["id"] == node_id), None)

        if not node_data:
            await websocket.send_json({"type": "error", "message": f"未找到节点 {node_id}"})
            await websocket.close()
            return

        node_type = node_data.get("type", "")
        current_config = node_data.get("config", {})

        nt_registry = session.exec(
            select(NodeTypeRegistry).where(NodeTypeRegistry.node_type == node_type)
        ).first()

        config_schema = nt_registry.config_schema if nt_registry else "{}"
        display_name = nt_registry.display_name if nt_registry else node_type

    # Resolve model from DAG's M node; fallback to the configured GLM default.
    llm_model_name = "glm-4-flash"
    llm_provider = "glm"
    for n in graph.get("nodes", []):
        if n.get("type") == "m":
            m_cfg = n.get("config", {})
            if m_cfg.get("model_name"):
                llm_model_name = m_cfg["model_name"]
            if m_cfg.get("provider"):
                llm_provider = m_cfg["provider"]
            break

    # Connection-level state (D1/D7): in-memory only, reset on reconnect.
    pending_config: dict = {}
    turn_counter: int = 0
    auto_commit: bool = True

    extraction_agent = create_agent(
        system_prompt=_build_extraction_prompt(config_schema),
        model_name=llm_model_name,
        provider=llm_provider,
        temperature=0.0,
        max_tokens=512,
    )

    await websocket.send_json({
        "type": "ready",
        "node_type": node_type,
        "display_name": display_name,
        "current_config": current_config,
    })

    try:
        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "message": "无效的 JSON 消息"})
                continue

            msg_type = msg.get("type")

            if msg_type == "set_auto_commit":
                auto_commit = bool(msg.get("value", True))
                continue

            if msg_type != "message":
                continue

            user_content = msg.get("content", "")
            if not user_content.strip():
                continue

            turn_counter += 1

            # B2: accumulate schema fields the user mentioned this turn into
            # pending_config (best-effort; silent on failure).
            extracted = await _extract_pending_fields(
                extraction_agent, user_content, config_schema
            )
            if extracted:
                pending_config.update(extracted)

            # B2: inject the accumulated pending_config into the system prompt
            # so the assistant sees previously collected fields.
            system_prompt = _build_node_config_prompt(
                display_name, config_schema, current_config, pending_config
            )
            chat_agent = create_agent(
                system_prompt=system_prompt,
                model_name=llm_model_name,
                provider=llm_provider,
                temperature=0.7,
                max_tokens=2048,
            )

            full_response = ""
            try:
                async for event_type, content in run_conversation(chat_agent, user_content):
                    await websocket.send_json({"type": event_type, "content": content})
                    if event_type == "token":
                        full_response += content
            except Exception as e:
                await websocket.send_json({"type": "error", "message": f"LLM 调用失败: {str(e)}"})
                continue

            # B1/B4: parse the assistant reply and either auto-commit or propose.
            parsed = merger.extract_config_block(full_response)
            if not parsed:
                continue

            filtered, warnings = merger.validate_against_schema(parsed, config_schema)
            for w in warnings:
                print(f"[node_config_chat] node={node_id} {w}", file=sys.stderr)
            if not filtered:
                continue

            if auto_commit:
                try:
                    new_version, changed_keys = merger.merge_into_graph(
                        agent_id, node_id, filtered, source="assistant_extracted"
                    )
                except ValueError as exc:
                    print(f"[node_config_chat] merge failed: {exc}", file=sys.stderr)
                    continue
                # Reflect the committed values locally so subsequent turns build
                # on the freshly written config.
                current_config = {**current_config, **filtered}
                await websocket.send_json({
                    "type": "node_config_committed",
                    "node_id": node_id,
                    "version": new_version,
                    "changed_keys": changed_keys,
                })
            else:
                changed_keys = [
                    k for k, v in filtered.items() if current_config.get(k) != v
                ]
                await websocket.send_json({
                    "type": "node_config_proposed",
                    "node_id": node_id,
                    "partial": filtered,
                    "changed_keys": changed_keys,
                })

    except Exception:
        pass
    finally:
        try:
            await websocket.close()
        except Exception:
            pass
