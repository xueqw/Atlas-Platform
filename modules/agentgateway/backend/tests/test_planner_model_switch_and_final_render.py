"""Backend tests for change: planner-model-switch-concurrency-and-final-render.

Covers:
- #1 能力库模型凭据透传：_resolve_model 带出 capability config 的 base_url/api_key；
  ModelRegistry 模型行为不变；create_agent 显式凭据覆盖环境变量默认。
- #2 切模型只改当前会话（后端部分）：_resolve_model 幂等，set_model 把凭据留在后端。
- #4 最终方案以文件呈现：proposal 文本被剥离、不进 messages（_strip_proposal_text）；
  流式阶段 ```json 围栏被 _ProposalJsonStripper 缓冲不外发，done 兜底丢弃。
"""

from __future__ import annotations

import json

import pytest
from sqlmodel import Session

from app.api import planner as planner_api
from app.models.db import CapabilityItem, ModelRegistry


# ─── #1 _resolve_model credential passthrough ────────────────────────────────


class TestResolveModelCredentials:
    def test_capability_model_carries_base_url_and_api_key(self):
        with Session(planner_api.engine) as session:
            session.add(CapabilityItem(
                type="model",
                name="gpt-5.4",
                description="custom endpoint",
                config=json.dumps({
                    "model_id": "gpt-5.4",
                    "provider": "openai",
                    "base_url": "https://custom.example.com/v1",
                    "api_key": "valid-custom-token-not-placeholder",
                }, ensure_ascii=False),
            ))
            session.commit()

        resolved = planner_api._resolve_model("gpt-5.4")
        assert resolved["model_name"] == "gpt-5.4"
        assert resolved["provider"] == "openai"
        assert resolved["base_url"] == "https://custom.example.com/v1"
        assert resolved["api_key"] == "valid-custom-token-not-placeholder"

    def test_capability_model_without_creds_omits_keys(self):
        with Session(planner_api.engine) as session:
            session.add(CapabilityItem(
                type="model",
                name="bare-model",
                config=json.dumps({"model_id": "bare-model", "provider": "glm"}, ensure_ascii=False),
            ))
            session.commit()

        resolved = planner_api._resolve_model("bare-model")
        assert resolved["model_name"] == "bare-model"
        assert resolved["provider"] == "glm"
        assert "base_url" not in resolved
        assert "api_key" not in resolved

    def test_model_registry_model_keeps_existing_behavior(self):
        with Session(planner_api.engine) as session:
            session.add(ModelRegistry(
                provider="glm",
                model_id="qwen3.6-27b",
                display_name="Qwen 3.6 27B",
            ))
            session.commit()

        resolved = planner_api._resolve_model("qwen3.6-27b")
        # Built-in models resolve to provider only — never carry inline creds.
        assert resolved == {"model_name": "qwen3.6-27b", "provider": "glm"}

    def test_unknown_model_falls_back_to_glm(self):
        resolved = planner_api._resolve_model("never-seen-model")
        assert resolved == {"model_name": "never-seen-model", "provider": "glm"}


class TestCreateAgentCredentialOverride:
    """create_agent must prefer explicit base_url/api_key over env-var defaults,
    and fall back to provider creds when they are omitted."""

    def test_explicit_credentials_override_provider_defaults(self, monkeypatch):
        captured = {}

        class _FakeCredential:
            def __init__(self, api_key=None, base_url=None):
                captured["api_key"] = api_key
                captured["base_url"] = base_url

        class _FakeParams:
            def __init__(self, **kw):
                pass

        class _FakeModel:
            Parameters = _FakeParams

            def __init__(self, credential=None, model=None, parameters=None, stream=True):
                captured["model"] = model

        class _FakeAgent:
            def __init__(self, name=None, system_prompt=None, model=None):
                pass

        import app.core.agentscope_runner as runner
        monkeypatch.setattr("agentscope.credential.OpenAICredential", _FakeCredential)
        monkeypatch.setattr("agentscope.model.OpenAIChatModel", _FakeModel)
        monkeypatch.setattr("agentscope.agent.Agent", _FakeAgent)
        monkeypatch.setattr(
            "app.core.config.get_provider_credentials",
            lambda provider: {"api_key": "ENV_KEY", "base_url": "https://env.example.com"},
        )

        runner.create_agent(
            system_prompt="hi",
            model_name="gpt-5.4",
            provider="openai",
            base_url="https://custom.example.com/v1",
            api_key="sk-secret-123",
        )
        assert captured["api_key"] == "sk-secret-123"
        assert captured["base_url"] == "https://custom.example.com/v1"
        assert captured["model"] == "gpt-5.4"

    def test_missing_credentials_fall_back_to_provider(self, monkeypatch):
        captured = {}

        class _FakeCredential:
            def __init__(self, api_key=None, base_url=None):
                captured["api_key"] = api_key
                captured["base_url"] = base_url

        class _FakeParams:
            def __init__(self, **kw):
                pass

        class _FakeModel:
            Parameters = _FakeParams

            def __init__(self, credential=None, model=None, parameters=None, stream=True):
                pass

        class _FakeAgent:
            def __init__(self, name=None, system_prompt=None, model=None):
                pass

        import app.core.agentscope_runner as runner
        monkeypatch.setattr("agentscope.credential.OpenAICredential", _FakeCredential)
        monkeypatch.setattr("agentscope.model.OpenAIChatModel", _FakeModel)
        monkeypatch.setattr("agentscope.agent.Agent", _FakeAgent)
        monkeypatch.setattr(
            "app.core.config.get_provider_credentials",
            lambda provider: {"api_key": "ENV_KEY", "base_url": "https://env.example.com"},
        )

        runner.create_agent(system_prompt="hi", model_name="qwen3.6-27b", provider="glm")
        assert captured["api_key"] == "ENV_KEY"
        assert captured["base_url"] == "https://env.example.com"


# ─── #4 final proposal not printed / not persisted ───────────────────────────


class TestProposalTextStripping:
    def test_strip_proposal_text_removes_json_fence(self):
        text = (
            "好的，这是最终方案：\n\n"
            "```json\n{\"ready\": true, \"proposal\": {\"architecture_summary\": \"x\"}}\n```\n"
        )
        prose = planner_api._strip_proposal_text(text)
        assert "好的，这是最终方案" in prose
        assert "{" not in prose
        assert "ready" not in prose

    def test_strip_proposal_text_bare_json_yields_empty(self):
        text = '{"ready": true, "proposal": {"architecture_summary": "x", "nodes": [], "edges": []}}'
        assert planner_api._strip_proposal_text(text) == ""

    def test_proposal_persisted_message_has_marker_not_json(self):
        # Simulate what the WS done-handler persists for a proposal turn.
        clean_text = (
            "方案如下：\n```json\n"
            '{"ready": true, "proposal": {"architecture_summary": "P+Agent+M", "nodes": [], "edges": []}}'
            "\n```"
        )
        structured = planner_api._try_extract_proposal(clean_text)
        assert structured is not None
        prose = planner_api._strip_proposal_text(clean_text)
        persisted = (prose + "\n\n" + planner_api._PROPOSAL_FILE_MARKER).strip() if prose else planner_api._PROPOSAL_FILE_MARKER
        assert planner_api._PROPOSAL_FILE_MARKER in persisted
        assert "ready" not in persisted
        assert "{" not in persisted
        assert "方案如下" in persisted


class TestProposalJsonStripper:
    def test_holds_json_fence_block(self):
        s = planner_api._ProposalJsonStripper()
        out = s.feed("方案就绪。")
        out += s.feed("```json\n{\"ready\": true}")
        out += s.feed("\n```")
        # Prose before the fence is emitted; the fenced JSON is withheld.
        assert "方案就绪。" in out
        assert "```json" not in out
        assert "ready" not in out
        assert s.inside is True
        assert "```json" in s.held

    def test_split_fence_across_tokens(self):
        s = planner_api._ProposalJsonStripper()
        out = s.feed("text ``")
        out += s.feed("`json\n{}")
        # The half fence must not leak as visible text.
        assert "```json" not in out
        assert "json" not in out.replace("text", "")  # only the prose "text" remains visible-ish
        assert s.inside is True

    def test_no_fence_emits_everything(self):
        s = planner_api._ProposalJsonStripper()
        out = s.feed("普通回复，没有 JSON。")
        out += s.flush()
        assert out == "普通回复，没有 JSON。"
        assert s.inside is False

    def test_legitimate_codeblock_recoverable_via_held(self):
        # When the assembled text is NOT a proposal, the held block can be
        # re-emitted by the caller so a real code block isn't lost.
        s = planner_api._ProposalJsonStripper()
        s.feed("see code:")
        s.feed("```json\n{\"some\": \"data\"}\n```")
        s.flush()
        assert s.held.startswith("```json")
        assert "some" in s.held


# ─── set_model resolution keeps creds backend-only ───────────────────────────


class TestSetModelResolution:
    def test_set_model_resolution_returns_creds_for_capability_model(self):
        with Session(planner_api.engine) as session:
            session.add(CapabilityItem(
                type="model",
                name="switch-target",
                config=json.dumps({
                    "model_id": "switch-target",
                    "provider": "openai",
                    "base_url": "https://switch.example.com/v1",
                    "api_key": "valid-switch-token-not-placeholder",
                }, ensure_ascii=False),
            ))
            session.commit()

        # set_model branch stores _resolve_model(model_id) into conv_data["model"].
        resolved = planner_api._resolve_model("switch-target")
        conv_data = {"model": resolved}
        # The public model_resolved event echoes only model_name/provider.
        public = {"model": conv_data["model"]["model_name"], "provider": conv_data["model"]["provider"]}
        assert public == {"model": "switch-target", "provider": "openai"}
        assert "api_key" not in public and "base_url" not in public
        # But the backend retains creds for the next-turn create_agent call.
        assert conv_data["model"]["api_key"] == "valid-switch-token-not-placeholder"
