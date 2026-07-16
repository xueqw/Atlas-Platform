"""Extension node types: K, T, C, X, H, A."""

from typing import Any, Dict
import json

from app.core.nodes.base import BaseNode, register_node


@register_node
class KNode(BaseNode):
    node_type = "k"
    display_name = "知识库"
    category = "knowledge"
    config_schema = json.dumps({
        "type": "object",
        "properties": {
            "collection_name": {"type": "string", "title": "知识库名称"},
            "top_k": {"type": "integer", "title": "返回条数", "default": 5},
        }
    })
    input_keys = ["user_query"]
    output_keys = ["retrieved_docs"]

    async def run(self, inputs: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
        query = inputs.get("user_query", "")
        return {"retrieved_docs": f"[RAG stub] Retrieved context for: {query}"}


@register_node
class TNode(BaseNode):
    node_type = "t"
    display_name = "工具"
    category = "tool"
    config_schema = json.dumps({
        "type": "object",
        "properties": {
            "tool_name": {"type": "string", "title": "工具名称"},
            "tool_params": {"type": "string", "title": "工具参数JSON"},
        }
    })
    input_keys = ["raw_response"]
    output_keys = ["tool_result"]

    async def run(self, inputs: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
        return {"tool_result": f"[Tool stub] Called {self.config.get('tool_name', 'unknown')}"}


@register_node
class CNode(BaseNode):
    node_type = "c"
    display_name = "条件路由"
    category = "flow"
    config_schema = json.dumps({
        "type": "object",
        "required": ["mode"],
        "properties": {
            "mode": {"type": "string", "title": "决策模式", "enum": ["rule", "llm"]},
            "condition": {"type": "string", "title": "条件表达式 (rule模式)"},
        }
    })
    input_keys = ["raw_response"]
    output_keys = ["branch", "_active_path"]

    async def run(self, inputs: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
        mode = self.config.get("mode", "rule")
        if mode == "rule":
            # Evaluate simple expression from config
            condition = self.config.get("condition", "true")
            branch = "true" if condition.strip().lower() == "true" else "false"
        else:
            # LLM decision (stub - would call a model to decide)
            branch = "true"
        return {"branch": branch, "_active_path": branch}


@register_node
class XNode(BaseNode):
    node_type = "x"
    display_name = "代码执行"
    category = "flow"
    config_schema = json.dumps({
        "type": "object",
        "required": ["language", "code"],
        "properties": {
            "language": {"type": "string", "title": "语言", "enum": ["python", "javascript"]},
            "code": {"type": "string", "title": "代码内容"},
            "timeout": {"type": "integer", "title": "超时(秒)", "default": 30},
        }
    })
    input_keys = []
    output_keys = ["exec_result"]

    async def run(self, inputs: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
        return {"exec_result": f"[Code stub] {self.config.get('language', 'python')} execution result"}


@register_node
class HNode(BaseNode):
    node_type = "h"
    display_name = "HTTP 调用"
    category = "external"
    config_schema = json.dumps({
        "type": "object",
        "required": ["url", "method"],
        "properties": {
            "url": {"type": "string", "title": "URL"},
            "method": {"type": "string", "title": "HTTP方法", "enum": ["GET", "POST", "PUT", "DELETE"]},
            "headers": {"type": "string", "title": "Headers JSON"},
            "body_template": {"type": "string", "title": "Body模板"},
            "timeout": {"type": "integer", "title": "超时(秒)", "default": 30},
        }
    })
    input_keys = []
    output_keys = ["http_response"]

    async def run(self, inputs: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
        return {"http_response": f"[HTTP stub] {self.config.get('method', 'GET')} {self.config.get('url', '')}"}


@register_node
class ANode(BaseNode):
    node_type = "a"
    display_name = "人工审批"
    category = "flow"
    config_schema = json.dumps({
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "title": "审批提示"},
        }
    })
    input_keys = ["raw_response"]
    output_keys = ["approval_result"]

    async def run(self, inputs: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
        return {"approval_result": "[Approval stub] pending human review"}


@register_node
class MemNode(BaseNode):
    node_type = "mem"
    display_name = "记忆"
    category = "core"
    config_schema = json.dumps({
        "type": "object",
        "properties": {
            "strategy": {"type": "string", "title": "记忆策略", "enum": ["sliding_window", "consolidation", "full"]},
            "max_turns": {"type": "integer", "title": "最大轮数", "minimum": 1, "maximum": 100},
            "persist": {"type": "boolean", "title": "持久化"},
        }
    })
    input_keys = ["user_query"]
    output_keys = ["memory_context"]

    async def run(self, inputs: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
        # MVP: return conversation history from state as memory context
        history = state.get("_conversation_history", [])
        max_turns = self.config.get("max_turns", 20)
        recent = history[-max_turns:] if history else []
        context = "\n".join(f"[{m.get('role', '?')}]: {m.get('content', '')}" for m in recent)
        return {"memory_context": context}
