"""P0 regression tests for planner-created agents and DAG runtime wiring."""

from __future__ import annotations

import json
import uuid

import pytest
from sqlmodel import Session

from app.api.planner import apply as planner_apply
from app.api.planner.schemas import StartRequest
from app.core.dag_executor import DAGParser
from app.core.nodes.agent_node import AgentNode
from app.core.model_caps import (
    DEFAULT_CHAT_MODEL_ID,
    DEFAULT_CHAT_PROVIDER,
    _capability_endpoint,
    resolve_model_endpoint,
)
from app.core.config import credential_gap, looks_like_placeholder


def test_default_chat_model_is_available_glm_flash():
    """New planner sessions/default agent nodes must not default to unavailable OpenAI/Qwen ids."""
    assert DEFAULT_CHAT_MODEL_ID == "glm-4.7-flash"
    assert DEFAULT_CHAT_PROVIDER == "glm"
    assert StartRequest().model_id == DEFAULT_CHAT_MODEL_ID
    assert AgentNode(config={}).config.get("model_name", DEFAULT_CHAT_MODEL_ID) == DEFAULT_CHAT_MODEL_ID


def test_build_graph_json_repairs_missing_capability_edges_for_agent_runtime():
    nodes = [
        {"id": "p1", "type": "p", "config": {"role_name": "助手", "system_prompt": "x" * 220}},
        {"id": "m1", "type": "m", "config": {"model_name": DEFAULT_CHAT_MODEL_ID, "provider": DEFAULT_CHAT_PROVIDER}},
        {"id": "k1", "type": "k", "config": {"collection_name": "kb"}},
        {"id": "mem1", "type": "mem", "config": {"strategy": "sliding_window"}},
        {"id": "agent1", "type": "agent", "config": {"system_prompt": "x" * 220, "model_name": DEFAULT_CHAT_MODEL_ID, "provider": DEFAULT_CHAT_PROVIDER}},
        {"id": "o1", "type": "o", "config": {}},
    ]

    graph = json.loads(planner_apply._build_graph_json(nodes, []))
    edge_pairs = {(e["source"], e["target"]) for e in graph["edges"]}

    # Capability/input nodes must feed the agent before it runs; otherwise DAGRunner
    # executes all nodes in one parallel level and AgentNode sees empty inputs.
    assert ("p1", "agent1") in edge_pairs
    assert ("m1", "agent1") in edge_pairs
    assert ("k1", "agent1") in edge_pairs
    assert ("mem1", "agent1") in edge_pairs
    assert ("agent1", "o1") in edge_pairs

    levels = DAGParser(json.dumps(graph)).topological_sort()
    assert levels.index(["agent1"]) > levels.index(["p1", "m1", "k1", "mem1"])
    assert levels[-1] == ["o1"]


# ---------------------------------------------------------------------------
# P0 Runtime Credential Resolution — placeholder fallback regression tests
# ---------------------------------------------------------------------------

class TestPlaceholderCapabilityIgnored:
    """Capability model config placeholder api_key/base_url MUST NOT override
    real provider environment credentials (root cause of Workbench test-chat
    failure: glm-4.7-flash cap config carried 'sk-your-glm-api-key' which
    shadowed the real GLM_API_KEY from env)."""

    def test_capability_endpoint_excludes_placeholder_api_key(self):
        """_capability_endpoint must not return a placeholder api_key."""
        cfg = {
            "model_id": "glm-4.7-flash",
            "provider": "glm",
            "api_key": "sk-your-glm-api-key",  # known .env.example placeholder
            "base_url": "http://real-gateway/v1",
        }
        ep = _capability_endpoint(cfg)
        # placeholder api_key must be absent from the result
        assert "api_key" not in ep
        # valid base_url must still be present
        assert ep.get("base_url") == "http://real-gateway/v1"

    def test_capability_endpoint_excludes_placeholder_base_url(self):
        """_capability_endpoint filters placeholder base_url too (edge case)."""
        cfg = {
            "model_id": "test-model",
            "provider": "openai",
            "api_key": "sk-proj-" + "a1b2c3" * 8,  # long enough = real key
            "base_url": "__set_via_env_only__",  # placeholder
        }
        ep = _capability_endpoint(cfg)
        assert "base_url" not in ep
        assert ep.get("api_key") == cfg["api_key"]

    def test_credential_gap_falls_through_placeholder_endpoint_to_env(self, monkeypatch):
        """When capability endpoint has a placeholder api_key, credential_gap
        should resolve using the env key and report no gap."""
        monkeypatch.setenv("GLM_API_KEY", "sk-real-" + "a1b2c3" * 8)
        monkeypatch.setenv("GLM_BASE_URL", "http://gateway/v1")
        # Simulate what _capability_endpoint would return AFTER the fix:
        # placeholder excluded → endpoint has no api_key → env fills in
        gap = credential_gap("glm", {"base_url": "http://gateway/v1"})
        assert gap is None

    def test_credential_gap_reports_gap_when_both_placeholder(self, monkeypatch):
        """If both capability and env are placeholder, credential_gap MUST report."""
        monkeypatch.setenv("GLM_API_KEY", "sk-your-glm-api-key")
        monkeypatch.setenv("GLM_BASE_URL", "http://gw/v1")
        gap = credential_gap("glm", {})  # no capability endpoint override
        assert gap is not None
        assert "api_key_placeholder" in gap["missing"]


class TestValidCustomCapabilityEndpointWins:
    """Non-placeholder capability endpoint credentials MUST take precedence
    over provider env defaults."""

    def test_real_capability_key_preserved(self):
        """A long, real-looking api_key in capability config is returned."""
        real_key = "sk-proj-" + "a1b2c3" * 8
        cfg = {
            "model_id": "custom-model",
            "provider": "openai",
            "api_key": real_key,
            "base_url": "https://custom.example/v1",
        }
        ep = _capability_endpoint(cfg)
        assert ep["api_key"] == real_key
        assert ep["base_url"] == "https://custom.example/v1"

    def test_resolve_model_endpoint_with_real_custom_cap(self):
        """resolve_model_endpoint includes custom endpoint from capability."""
        from app.core.database import engine
        from app.models.db import CapabilityItem

        mid = f"custom-ep-{uuid.uuid4().hex[:8]}"
        real_key = "sk-proj-" + "a1b2c3" * 8
        with Session(engine) as s:
            s.add(CapabilityItem(
                type="model", name=f"Cap {mid}",
                config=json.dumps({
                    "model_id": mid, "provider": "openai",
                    "base_url": "https://custom.example/v1",
                    "api_key": real_key,
                }),
                tags="[]",
            ))
            s.commit()
            result = resolve_model_endpoint(mid, session=s)
        assert result["provider"] == "openai"
        assert result["base_url"] == "https://custom.example/v1"
        assert result["api_key"] == real_key


class TestAgentNodeEndpointResolutionNoSecretLeak:
    """AgentNode runtime endpoint resolution for glm-4.7-flash must not
    emit api_key_placeholder when real env credentials exist, and must
    never leak secret values in error messages."""

    @pytest.mark.asyncio
    async def test_glm_flash_with_placeholder_cap_uses_env(self, monkeypatch):
        """glm-4.7-flash capability has placeholder key → runtime uses env."""
        import types
        from app.core import agentscope_runner as runner
        from app.core.database import engine
        from app.models.db import CapabilityItem

        real_key = "sk-real-" + "a1b2c3" * 8
        monkeypatch.setenv("GLM_API_KEY", real_key)
        monkeypatch.setenv("GLM_BASE_URL", "http://gateway/v1")

        # Insert a capability with placeholder api_key
        mid = "glm-4.7-flash"
        with Session(engine) as s:
            # Clean up any existing capability for this model_id in test
            from sqlmodel import select
            from app.models.db import CapabilityItem
            existing = s.exec(
                select(CapabilityItem).where(CapabilityItem.type == "model")
            ).all()
            for cap in existing:
                cfg = json.loads(cap.config) if isinstance(cap.config, str) else (cap.config or {})
                if isinstance(cfg, dict) and cfg.get("model_id") == mid:
                    s.delete(cap)
            s.add(CapabilityItem(
                type="model", name="GLM Flash Placeholder",
                config=json.dumps({
                    "model_id": mid, "provider": "glm",
                    "api_key": "sk-your-glm-api-key",  # placeholder!
                    "base_url": "http://gateway/v1",
                }),
                tags="[]",
            ))
            s.commit()

        captured = {}

        def _create_agent(system_prompt, model_name, provider="glm",
                          base_url=None, api_key=None, **kwargs):
            captured.update(provider=provider, base_url=base_url,
                            api_key=api_key, model_name=model_name)
            return types.SimpleNamespace()

        def _run_conversation(agent, user_message, attachments=None):
            async def _gen():
                yield ("token", "ok")
            return _gen()

        monkeypatch.setattr(runner, "create_agent", _create_agent)
        monkeypatch.setattr(runner, "run_conversation", _run_conversation)

        node = AgentNode(config={
            "system_prompt": "hi",
            "model_name": mid,
            "provider": "glm",
        })
        # Fresh state, no cache
        await node.run({"user_query": "hello"}, {})

        # The node must NOT pass the placeholder to create_agent
        assert captured["api_key"] != "sk-your-glm-api-key"
        # It should either pass None (env lookup happens in runner) or the env key
        # The node must not raise RuntimeError about api_key_placeholder
        assert captured["provider"] == "glm"

    @pytest.mark.asyncio
    async def test_error_message_never_contains_key_values(self, monkeypatch):
        """When credential gap is detected, error message names the field
        but never the actual key value."""
        monkeypatch.setenv("CUSTOM_API_KEY", "")
        monkeypatch.setenv("CUSTOM_BASE_URL", "")

        node = AgentNode(config={
            "system_prompt": "hi",
            "model_name": "no-such-model",
            "provider": "custom",
        })
        with pytest.raises(RuntimeError) as exc:
            await node.run(
                {"user_query": "q"},
                {"_model_endpoint_cache": {"no-such-model": {"provider": "custom"}}},
            )
        msg = str(exc.value)
        assert "api_key" in msg
        assert "custom" in msg
        # No secret material
        assert "sk-" not in msg
