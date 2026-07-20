"""AI Agent node — a self-contained agent with built-in persona + model.

Can optionally receive system_prompt from an external P node (overrides built-in).
Can optionally receive model config from state if connected to an M node.
"""

from typing import Any, Dict
import json

from app.core.nodes.base import BaseNode, register_node
from app.core.model_caps import DEFAULT_CHAT_MODEL_ID, DEFAULT_CHAT_PROVIDER


@register_node
class AgentNode(BaseNode):
    node_type = "agent"
    display_name = "AI Agent"
    category = "core"
    config_schema = json.dumps({
        "type": "object",
        "required": ["system_prompt", "model_name", "provider"],
        "properties": {
            "role_name": {"type": "string", "title": "角色名称"},
            "system_prompt": {"type": "string", "title": "系统提示词"},
            "output_format": {"type": "string", "title": "输出格式", "enum": ["markdown", "json", "text"]},
            "model_name": {"type": "string", "title": "模型名称"},
            "provider": {"type": "string", "title": "提供商", "enum": ["openai", "anthropic", "deepseek", "glm", "custom"]},
            "temperature": {"type": "number", "title": "温度", "minimum": 0, "maximum": 2},
            "max_tokens": {"type": "integer", "title": "最大Token数", "minimum": 256, "maximum": 128000},
            "streaming": {"type": "boolean", "title": "流式输出"},
        }
    })
    input_keys = ["user_query", "system_prompt"]
    output_keys = ["raw_response"]

    async def run(self, inputs: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
        from app.core.agentscope_runner import create_agent, run_conversation

        # Use external system_prompt if provided (from P node), otherwise use built-in
        external_prompt = inputs.get("system_prompt", "")
        built_in_prompt = self.config.get("system_prompt", "")
        role_name = self.config.get("role_name", "")

        system_prompt = external_prompt or built_in_prompt
        if role_name and not external_prompt:
            system_prompt = f"# 角色: {role_name}\n\n{system_prompt}"

        model_name = self.config.get("model_name", DEFAULT_CHAT_MODEL_ID)

        # Authoritative routing: resolve provider (+ any capability-carried
        # base_url/api_key) from model_name rather than trusting the node's
        # static config.provider, which may be stale/mismatched after a model
        # switch in the editor. Cached per DAG run via the shared state dict so
        # repeated agent nodes don't re-query. Falls back to config.provider when
        # no DB session is available (e.g. isolated debug-node runs).
        endpoint = self._resolve_endpoint(model_name, state)
        provider = endpoint.get("provider") or self.config.get("provider", DEFAULT_CHAT_PROVIDER)

        # Visible attribution: if this provider has no usable credentials, fail
        # with a named reason rather than letting the gateway return a bare 401
        # (or silently yielding an empty response). Never leaks key values.
        from app.core.config import credential_gap
        gap = credential_gap(provider, {k: endpoint[k] for k in ("base_url", "api_key") if k in endpoint})
        if gap is not None:
            event_hook = state.get("_event_hook")
            miss = "/".join(gap["missing"])
            reason = f"模型 {model_name} 的 provider「{provider}」缺少凭据（{miss}），请检查 .env"
            if event_hook is not None:
                await event_hook({"type": "error", "content": reason})
            raise RuntimeError(reason)

        agent = create_agent(
            system_prompt=system_prompt,
            model_name=model_name,
            provider=provider,
            stream=True,
            temperature=self.config.get("temperature", 0.7),
            max_tokens=self.config.get("max_tokens", 4096),
            base_url=endpoint.get("base_url"),
            api_key=endpoint.get("api_key"),
        )

        # The DAGRunner hands a per-node event hook via state so the node can
        # stream tokens to the chat WebSocket. When absent (evaluation path),
        # the node just accumulates the response silently.
        event_hook = state.get("_event_hook")
        user_query = inputs.get("user_query", "")

        full_response = ""
        async for event_type, content in run_conversation(agent, user_query):
            if event_type == "thinking_content":
                if event_hook is not None:
                    await event_hook({"type": "thinking_content", "content": content})
            elif event_type == "token":
                full_response += content
                if event_hook is not None:
                    await event_hook({"type": "token", "content": content})

        return {"raw_response": full_response.strip()}

    @staticmethod
    def _resolve_endpoint(model_name: str, state: Dict[str, Any]) -> Dict[str, Any]:
        """Resolve {provider, base_url?, api_key?} for ``model_name``.

        Result is memoized in the shared run state under
        ``_model_endpoint_cache`` so a DAG with several agent nodes on the same
        model only hits the DB once. Returns an empty dict on any failure so the
        caller falls back to the node's static config.provider.
        """
        # A debug-node run is intentionally isolated from a persisted DAG and
        # must honour the provider explicitly supplied in the debug payload.
        # The API marks that context so a model with the same name in the
        # capability registry cannot silently replace the requested endpoint.
        if isinstance(state, dict) and state.get("_isolated_debug"):
            return {}

        cache = state.setdefault("_model_endpoint_cache", {}) if isinstance(state, dict) else {}
        if model_name in cache:
            return cache[model_name]
        endpoint: Dict[str, Any] = {}
        try:
            from sqlmodel import Session
            from app.core.database import engine
            from app.core.model_caps import resolve_model_endpoint
            with Session(engine) as session:
                endpoint = resolve_model_endpoint(model_name, session=session)
        except Exception:
            endpoint = {}
        cache[model_name] = endpoint
        return endpoint
