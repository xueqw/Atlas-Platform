"""Model provider routing: save-graph normalize + DAG exec credential passthrough.

Covers fix-model-switch-provider-routing groups 2 & 3:
- save_dag_graph rewrites each model-bearing node's provider to the authoritative
  value resolved from model_name (and is idempotent on already-correct graphs).
- AgentNode execution resolves {provider, base_url?, api_key?} from model_name and
  passes the capability-carried endpoint through to create_agent, overriding a
  stale/mismatched config.provider.
"""

from __future__ import annotations

import json
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.api import dag as dag_api
from app.api import agents as agents_api
from app.core.database import engine


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(agents_api.router, prefix="/api")
    app.include_router(dag_api.router, prefix="/api")
    return TestClient(app)


def _agent_graph(model_name: str, provider: str) -> str:
    return json.dumps({
        "nodes": [
            {"id": "i1", "type": "i", "config": {}},
            {"id": "a1", "type": "agent", "config": {
                "system_prompt": "你是助手。" * 4,
                "model_name": model_name, "provider": provider,
            }},
            {"id": "o1", "type": "o", "config": {}},
        ],
        "edges": [
            {"id": "e1", "source": "i1", "target": "a1"},
            {"id": "e2", "source": "a1", "target": "o1"},
        ],
    }, ensure_ascii=False)


def _new_agent(c: TestClient) -> int:
    res = c.post("/api/agents", json={"name": "route-test", "description": "x"})
    assert res.status_code == 200
    return res.json()["id"]


def _saved_agent_node_provider(c: TestClient, agent_id: int) -> str:
    res = c.get(f"/api/agents/{agent_id}/dag-graph")
    assert res.status_code == 200
    nodes = json.loads(res.json()["graph_json"])["nodes"]
    a = next(n for n in nodes if n["type"] == "agent")
    return a["config"]["provider"]


class TestSaveGraphNormalizesProvider:
    def test_mismatched_provider_corrected_on_save(self, client):
        # gpt-* prefix → openai; saving with a wrong provider must be corrected.
        agent_id = _new_agent(client)
        res = client.post(f"/api/agents/{agent_id}/dag-graph",
                          json={"graph_json": _agent_graph("gpt-4o", "glm"), "state_schema": "{}"})
        assert res.status_code == 200
        assert _saved_agent_node_provider(client, agent_id) == "openai"

    def test_correct_provider_is_idempotent(self, client):
        agent_id = _new_agent(client)
        # qwen* → glm; already correct, must stay unchanged.
        graph = _agent_graph("qwen3.6-27b", "glm")
        res = client.post(f"/api/agents/{agent_id}/dag-graph",
                          json={"graph_json": graph, "state_schema": "{}"})
        assert res.status_code == 200
        assert _saved_agent_node_provider(client, agent_id) == "glm"


class TestAgentNodePassesThroughCredentials:
    """AgentNode.run resolves routing from model_name and passes capability
    endpoints to create_agent, overriding a stale config.provider."""

    def _patch_create_agent(self, monkeypatch):
        import types
        from app.core import agentscope_runner as runner
        captured = {}

        def _create_agent(system_prompt, model_name, provider="glm", base_url=None,
                          api_key=None, **kwargs):
            captured.update(provider=provider, base_url=base_url, api_key=api_key,
                            model_name=model_name)
            return types.SimpleNamespace()

        def _run_conversation(agent, user_message, attachments=None):
            async def _gen():
                yield ("token", "ok")
            return _gen()

        monkeypatch.setattr(runner, "create_agent", _create_agent)
        monkeypatch.setattr(runner, "run_conversation", _run_conversation)
        return captured

    @pytest.mark.asyncio
    async def test_capability_endpoint_passed_through(self, monkeypatch):
        from app.models.db import CapabilityItem
        from app.core.nodes.agent_node import AgentNode

        mid = f"cap-exec-{uuid.uuid4().hex[:8]}"
        with Session(engine) as s:
            s.add(CapabilityItem(
                type="model", name="Exec Cap",
                config=json.dumps({
                    "model_id": mid, "provider": "openai",
                    "base_url": "https://exec.example/v1", "api_key": "sk-exec-a1b2c3a1b2c3a1b2c3a1b2c3a1b2c3a1b2c3",
                }),
                tags="[]",
            ))
            s.commit()

        captured = self._patch_create_agent(monkeypatch)
        node = AgentNode(config={"system_prompt": "hi", "model_name": mid, "provider": "glm"})
        await node.run({"user_query": "q"}, {})

        # config.provider was glm but model_name resolves to the capability's
        # openai endpoint — execution must use the resolved routing.
        assert captured["provider"] == "openai"
        assert captured["base_url"] == "https://exec.example/v1"
        assert captured["api_key"] == "sk-exec-a1b2c3a1b2c3a1b2c3a1b2c3a1b2c3a1b2c3"

    @pytest.mark.asyncio
    async def test_stale_config_provider_overridden_by_model_name(self, monkeypatch):
        from app.core.nodes.agent_node import AgentNode

        captured = self._patch_create_agent(monkeypatch)
        # gpt-* → openai by prefix; config says glm (stale).
        node = AgentNode(config={"system_prompt": "hi", "model_name": "gpt-4o", "provider": "glm"})
        await node.run({"user_query": "q"}, {})
        assert captured["provider"] == "openai"
        # Registry/built-in model carries no custom endpoint.
        assert captured["base_url"] is None and captured["api_key"] is None

    @pytest.mark.asyncio
    async def test_endpoint_cache_reused_within_run(self, monkeypatch):
        from app.core.nodes.agent_node import AgentNode

        self._patch_create_agent(monkeypatch)
        calls = {"n": 0}
        import app.core.model_caps as mc
        real = mc.resolve_model_endpoint

        def _counting(model_name, session=None, default="glm"):
            calls["n"] += 1
            return real(model_name, session=session, default=default)

        monkeypatch.setattr(mc, "resolve_model_endpoint", _counting)
        state: dict = {}
        node = AgentNode(config={"system_prompt": "hi", "model_name": "gpt-4o", "provider": "glm"})
        await node.run({"user_query": "q1"}, state)
        await node.run({"user_query": "q2"}, state)  # same shared state
        assert calls["n"] == 1  # second run hits the cache


class TestCredentialGap:
    """config.credential_gap: structured, key-free missing-credential reporting."""

    def test_present_key_no_gap(self, monkeypatch):
        from app.core import config
        monkeypatch.setenv("GLM_API_KEY", "sk-present-a1b2c3a1b2c3a1b2c3a1b2c3a1b2c3a1b2c3")
        monkeypatch.setenv("GLM_BASE_URL", "http://gw/v1")
        assert config.credential_gap("glm") is None

    def test_missing_key_reported(self, monkeypatch):
        from app.core import config
        monkeypatch.setenv("GLM_API_KEY", "")
        monkeypatch.setenv("GLM_BASE_URL", "http://gw/v1")
        gap = config.credential_gap("glm")
        assert gap == {"provider": "glm", "missing": ["api_key"]}

    def test_endpoint_overrides_env(self, monkeypatch):
        from app.core import config
        monkeypatch.setenv("GLM_API_KEY", "")
        # capability-carried endpoint supplies the key → no gap
        assert config.credential_gap("glm", {"api_key": "sk-cap-a1b2c3a1b2c3a1b2c3a1b2c3a1b2c3a1b2c3", "base_url": "http://x/v1"}) is None

    def test_anthropic_no_base_url_not_flagged(self, monkeypatch):
        from app.core import config
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-a1b2c3a1b2c3a1b2c3a1b2c3a1b2c3a1b2c3a1b2c3a1b2c3")
        # anthropic has no base_url_env → missing base_url is not a gap
        assert config.credential_gap("anthropic") is None

    def test_gap_never_contains_key_value(self, monkeypatch):
        from app.core import config
        monkeypatch.setenv("GLM_API_KEY", "")
        gap = config.credential_gap("glm")
        assert "sk-" not in json.dumps(gap)
        assert set(gap.keys()) == {"provider", "missing"}

    def test_placeholder_key_counts_as_gap(self, monkeypatch):
        from app.core import config
        # 13-char sk- stub (the observed bad value) is non-empty but a placeholder.
        monkeypatch.setenv("OPENAI_API_KEY", "sk-1234567890")
        monkeypatch.setenv("OPENAI_BASE_URL", "https://aizzz.org/v1")
        gap = config.credential_gap("openai")
        assert gap is not None
        assert gap["provider"] == "openai"
        assert "api_key_placeholder" in gap["missing"]
        assert "sk-" not in json.dumps(gap)

    def test_real_key_no_gap(self, monkeypatch):
        from app.core import config
        monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-" + "a1b2c3" * 8)
        monkeypatch.setenv("OPENAI_BASE_URL", "https://aizzz.org/v1")
        assert config.credential_gap("openai") is None


class TestExecMissingCredentialAttribution:
    """AgentNode.run surfaces a named reason (not a silent empty response or
    bare 401) when the resolved provider has no usable credentials."""

    @pytest.mark.asyncio
    async def test_missing_cred_raises_named_reason(self, monkeypatch):
        from app.core.nodes.agent_node import AgentNode

        # custom provider with empty env creds and no capability endpoint.
        monkeypatch.setenv("CUSTOM_API_KEY", "")
        monkeypatch.setenv("CUSTOM_BASE_URL", "")
        events = []

        async def _hook(ev):
            events.append(ev)

        node = AgentNode(config={"system_prompt": "hi", "model_name": "mystery-x", "provider": "custom"})
        with pytest.raises(RuntimeError) as exc:
            await node.run({"user_query": "q"}, {"_event_hook": _hook, "_model_endpoint_cache": {"mystery-x": {"provider": "custom"}}})
        msg = str(exc.value)
        assert "custom" in msg and "api_key" in msg
        assert "sk-" not in msg  # no key value leaked
        assert any(e.get("type") == "error" for e in events)  # surfaced to stream


class TestProviderConflictAudit:
    """scripts.audit_model_providers: read-only conflict detection + opt-in fix."""

    def test_detects_conflict(self):
        from app.models.db import ModelRegistry, CapabilityItem
        from scripts.audit_model_providers import find_provider_conflicts

        mid = f"audit-{uuid.uuid4().hex[:8]}"
        with Session(engine) as s:
            s.add(ModelRegistry(
                provider="glm", model_id=mid, display_name="Audit Reg",
                capability_tags="[]", context_window=8192, max_output_tokens=4096,
                input_price_per_1k=0.0, output_price_per_1k=0.0,
                supports_streaming=True, supports_vision=False, is_available=True,
            ))
            s.add(CapabilityItem(
                type="model", name="Audit Cap",
                config=json.dumps({"model_id": mid, "provider": "openai"}), tags="[]",
            ))
            s.commit()
            conflicts = find_provider_conflicts(s)
        hit = [c for c in conflicts if c["model_id"] == mid]
        assert len(hit) == 1
        assert hit[0]["registry_provider"] == "glm"
        assert hit[0]["capability_provider"] == "openai"

    def test_fix_converges_capability_and_is_clean_after(self):
        from app.models.db import ModelRegistry, CapabilityItem
        from scripts.audit_model_providers import find_provider_conflicts, converge_to_registry

        mid = f"audit-fix-{uuid.uuid4().hex[:8]}"
        with Session(engine) as s:
            s.add(ModelRegistry(
                provider="anthropic", model_id=mid, display_name="Fix Reg",
                capability_tags="[]", context_window=8192, max_output_tokens=4096,
                input_price_per_1k=0.0, output_price_per_1k=0.0,
                supports_streaming=True, supports_vision=False, is_available=True,
            ))
            s.add(CapabilityItem(
                type="model", name="Fix Cap",
                config=json.dumps({"model_id": mid, "provider": "glm"}), tags="[]",
            ))
            s.commit()
            n = converge_to_registry(s, [c for c in find_provider_conflicts(s) if c["model_id"] == mid])
            assert n == 1
            remaining = [c for c in find_provider_conflicts(s) if c["model_id"] == mid]
            assert remaining == []
            # capability row now declares the registry provider
            cap = s.exec(select(CapabilityItem).where(CapabilityItem.name == "Fix Cap")).first()
            assert json.loads(cap.config)["provider"] == "anthropic"

    def test_no_conflict_when_aligned(self):
        from app.models.db import ModelRegistry, CapabilityItem
        from scripts.audit_model_providers import find_provider_conflicts

        mid = f"audit-ok-{uuid.uuid4().hex[:8]}"
        with Session(engine) as s:
            s.add(ModelRegistry(
                provider="glm", model_id=mid, display_name="OK Reg",
                capability_tags="[]", context_window=8192, max_output_tokens=4096,
                input_price_per_1k=0.0, output_price_per_1k=0.0,
                supports_streaming=True, supports_vision=False, is_available=True,
            ))
            s.add(CapabilityItem(
                type="model", name="OK Cap",
                config=json.dumps({"model_id": mid, "provider": "glm"}), tags="[]",
            ))
            s.commit()
            conflicts = [c for c in find_provider_conflicts(s) if c["model_id"] == mid]
        assert conflicts == []



