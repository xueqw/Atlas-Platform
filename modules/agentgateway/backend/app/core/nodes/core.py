"""Built-in core node types: P, M, I, O."""

from typing import Any, Dict
import json

from app.core.nodes.base import BaseNode, register_node
from app.core.agentscope_runner import create_agent, run_conversation
from app.core.model_caps import DEFAULT_CHAT_MODEL_ID, DEFAULT_CHAT_PROVIDER


@register_node
class PNode(BaseNode):
    node_type = "p"
    display_name = "提示词"
    category = "core"
    config_schema = json.dumps({
        "type": "object",
        "required": ["role_name", "system_prompt"],
        "properties": {
            "role_name": {"type": "string", "title": "角色名称"},
            "role_description": {"type": "string", "title": "角色描述"},
            "system_prompt": {"type": "string", "title": "系统提示词", "minLength": 200, "maxLength": 4000},
            "output_format": {"type": "string", "title": "输出格式", "enum": ["markdown", "json", "text"]},
        }
    })
    input_keys = ["user_query"]
    output_keys = ["system_prompt"]

    async def run(self, inputs: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
        template = self.config.get("system_prompt", "")
        role_name = self.config.get("role_name", "")
        role_desc = self.config.get("role_description", "")
        prompt = f"# 角色: {role_name}\n\n{role_desc}\n\n{template}" if role_name else template
        return {"system_prompt": prompt}


@register_node
class MNode(BaseNode):
    node_type = "m"
    display_name = "模型"
    category = "core"
    config_schema = json.dumps({
        "type": "object",
        "required": ["model_name", "provider"],
        "properties": {
            "model_name": {"type": "string", "title": "模型名称"},
            "provider": {"type": "string", "title": "提供商", "enum": ["openai", "anthropic", "deepseek"]},
            "temperature": {"type": "number", "title": "温度", "minimum": 0, "maximum": 2},
            "max_tokens": {"type": "integer", "title": "最大Token数", "minimum": 256, "maximum": 128000},
            "streaming": {"type": "boolean", "title": "流式输出"},
        }
    })
    input_keys = ["system_prompt", "user_query"]
    output_keys = ["raw_response"]

    async def run(self, inputs: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
        agent = create_agent(
            system_prompt=inputs.get("system_prompt", ""),
            model_name=self.config.get("model_name", DEFAULT_CHAT_MODEL_ID),
            provider=self.config.get("provider", DEFAULT_CHAT_PROVIDER),
            stream=True,
            temperature=self.config.get("temperature", 0.7),
            max_tokens=self.config.get("max_tokens", 4096),
        )
        response = ""
        async for msg_type, content in run_conversation(agent, inputs.get("user_query", "")):
            if msg_type == "token":
                response += content
        # Stream the model's response to the chat WS when a hook is present
        # (preserves the per-node token the inline executor used to emit).
        event_hook = state.get("_event_hook")
        if event_hook is not None and response:
            await event_hook({"type": "token", "content": response})
        return {"raw_response": response}


@register_node
class INode(BaseNode):
    node_type = "i"
    display_name = "入口"
    category = "core"
    config_schema = json.dumps({
        "type": "object",
        "properties": {
            "input_mode": {"type": "string", "title": "输入模式", "enum": ["text", "json_schema"]},
            "input_schema": {"type": "string", "title": "输入JSON Schema"},
            "placeholder": {"type": "string", "title": "占位提示"},
        }
    })
    input_keys = []
    output_keys = ["user_query"]

    async def run(self, inputs: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
        user_query = state.get("_raw_input", inputs.get("user_query", ""))
        return {"user_query": user_query}


@register_node
class ONode(BaseNode):
    node_type = "o"
    display_name = "出口"
    category = "core"
    config_schema = json.dumps({
        "type": "object",
        "properties": {
            "output_mode": {"type": "string", "title": "输出模式", "enum": ["text", "json_schema"]},
            "output_schema": {"type": "string", "title": "输出JSON Schema"},
        }
    })
    input_keys = ["raw_response"]
    output_keys = ["final_output"]

    async def run(self, inputs: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
        return {"final_output": inputs.get("raw_response", "")}
