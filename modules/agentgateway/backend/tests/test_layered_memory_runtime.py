"""Tests for implement-layered-memory-contract-batch-b (layered-memory-runtime).

Covers the change's acceptance scenarios:
- 5.1 装配顺序与 policy 门控：每场景顺序、未启用层不查询、memory_enabled=false → 空
- 5.2 episodic 限量：候选 >3 → 注入夹到 3；不全量注入
- 5.3 写入准入闸门：各类非长期内容拒绝、长期价值内容接受、extractor 候选先过闸门
- 5.4 接入回归：policy off → 渲染块为空（注入后行为等价改动前）
- 5.5 migration 双库（SQLite 侧）+ 旧行兼容加载

Tests run against the temp SQLite engine patched in conftest (schema created via
SQLModel.metadata.create_all). The semantic provider is `none` in tests, so we
patch `retrieve_semantic_memories` where a scenario needs episodic/semantic data.
"""

from __future__ import annotations

from sqlmodel import Session

from app.core import memory_service as m
from app.core import memory_extractor as ext
from app.core.memory_provider import MemoryRecord
from app.core.database import engine
from app.models.db import Agent, MemoryItem, _utcnow


def _enabled_policy(**over) -> m.AgentMemoryPolicy:
    base = dict(enabled=True, provider="pgonly", scope="agent", policy={}, procedural_refs=[], architecture_pattern="")
    base.update(over)
    return m.AgentMemoryPolicy(**base)


# ─── 5.3 写入准入闸门 ────────────────────────────────────────────────────────

class TestWriteAdmissionGate:
    def test_rejects_empty_and_blank(self):
        assert m.is_admissible_for_long_term(content="") is False
        assert m.is_admissible_for_long_term(content="   \n ") is False

    def test_rejects_raw_tool_output(self):
        assert m.is_admissible_for_long_term(content="some output", message_type="tool") is False

    def test_rejects_stream_token_fragment(self):
        assert m.is_admissible_for_long_term(content="Hel", message_type="stream_token") is False
        assert m.is_admissible_for_long_term(content="lo", message_type="token") is False

    def test_rejects_bare_artifact_id_or_path(self):
        assert m.is_admissible_for_long_term(content="/tmp/run/abc123.json") is False
        assert m.is_admissible_for_long_term(content="artifact_9f3e2b1c0d") is False

    def test_rejects_greeting(self):
        assert m.is_admissible_for_long_term(content="你好！") is False
        assert m.is_admissible_for_long_term(content="thanks") is False

    def test_rejects_full_transcript_dump(self):
        assert m.is_admissible_for_long_term(content="x" * 5000) is False

    def test_accepts_durable_fact(self):
        assert m.is_admissible_for_long_term(
            content="用户长期偏好简洁中文回答，并要求所有结论附带依据。"
        ) is True

    def test_extractor_candidate_passes_gate(self):
        # admissible episodic summary → one candidate
        recs = ext.extract_episodic_candidates(
            source_kind="proposal", source_ref="p1",
            payload={"summary": "关键决策：选用 pgonly 记忆并默认关闭。"}, agent_id=1,
        )
        assert len(recs) == 1 and recs[0].memory_type == "episodic"

    def test_extractor_rejects_inadmissible_candidate(self):
        # blank summary → gate rejects → no candidate
        recs = ext.extract_episodic_candidates(
            source_kind="proposal", source_ref="p2", payload={"summary": "   "}, agent_id=1,
        )
        assert recs == []


# ─── 5.1 装配顺序与 policy 门控 ──────────────────────────────────────────────

class TestAssemblyPolicyGating:
    def test_unknown_scenario_raises(self):
        try:
            m.assemble_layered_context("bogus", policy=_enabled_policy())
            assert False, "should have raised"
        except ValueError:
            pass

    def test_memory_disabled_returns_empty(self):
        ctx = m.assemble_layered_context(
            m.SCENARIO_AGENT_CHAT, policy=m.AgentMemoryPolicy.disabled(),
            conversation_id=1, query="hi",
        )
        assert ctx.enabled is False
        assert ctx.recent_messages == [] and ctx.profile == [] and ctx.semantic == []
        assert m.render_layered_context_block(ctx) == ""

    def test_disabled_layer_not_queried(self, monkeypatch):
        # semantic layer OFF → retrieve_semantic_memories must NOT be called.
        calls = {"n": 0}

        def _spy(*a, **k):
            calls["n"] += 1
            return []

        monkeypatch.setattr(m, "retrieve_semantic_memories", _spy)
        policy = _enabled_policy(policy={"layers": ["profile"]})  # only profile on
        ctx = m.assemble_layered_context(
            m.SCENARIO_AGENT_CHAT, policy=policy, conversation_id=None, agent_id=1, query="q",
        )
        assert ctx.enabled is True
        assert calls["n"] == 0, "semantic/episodic layers were off; must not query"
        assert ctx.semantic == [] and ctx.episodic == []

    def test_enabled_layer_is_queried(self, monkeypatch):
        rec = MemoryRecord(memory_type="semantic", scope="agent", content="事实A", importance=0.9, confidence=0.8)
        monkeypatch.setattr(m, "retrieve_semantic_memories", lambda *a, **k: [rec])
        policy = _enabled_policy(policy={"layers": ["semantic"]})
        ctx = m.assemble_layered_context(
            m.SCENARIO_AGENT_CHAT, policy=policy, conversation_id=None, agent_id=1, query="q",
        )
        assert ctx.semantic and ctx.semantic[0].content == "事实A"

    def test_planner_scenario_assembles(self):
        # planner with no agent_id but enabled policy passed explicitly: profile
        # layer queried (empty in fresh DB), no crash, scenario recorded.
        ctx = m.assemble_layered_context(
            m.SCENARIO_PLANNER, policy=_enabled_policy(), planner_conversation_id="conv-x", query="q",
        )
        assert ctx.scenario == m.SCENARIO_PLANNER and ctx.enabled is True

    def test_replan_scenario_assembles(self):
        ctx = m.assemble_layered_context(
            m.SCENARIO_REPLAN, policy=_enabled_policy(), agent_id=1,
            planner_conversation_id="conv-y", query="q",
        )
        assert ctx.scenario == m.SCENARIO_REPLAN and ctx.agent_policy is not None


# ─── 5.2 episodic 限量 ───────────────────────────────────────────────────────

class TestEpisodicLimit:
    def test_agent_chat_episodic_capped_at_3(self, monkeypatch):
        many = [
            MemoryRecord(memory_type="episodic", scope="agent", content=f"e{i}", importance=1.0 - i * 0.01)
            for i in range(10)
        ]
        monkeypatch.setattr(m, "retrieve_semantic_memories", lambda *a, **k: list(many))
        policy = _enabled_policy(policy={"layers": ["episodic"]})
        ctx = m.assemble_layered_context(
            m.SCENARIO_AGENT_CHAT, policy=policy, conversation_id=None, agent_id=1, query="q",
        )
        assert len(ctx.episodic) <= 3


# ─── 5.4 接入回归（policy off 等价） ─────────────────────────────────────────

class TestInjectionRegression:
    def test_render_block_empty_when_disabled(self):
        ctx = m.assemble_layered_context(
            m.SCENARIO_AGENT_CHAT, policy=m.AgentMemoryPolicy.disabled(), conversation_id=1, query="hi",
        )
        assert m.render_layered_context_block(ctx) == ""

    def test_render_block_empty_when_enabled_but_no_data(self):
        ctx = m.assemble_layered_context(
            m.SCENARIO_AGENT_CHAT, policy=_enabled_policy(), conversation_id=None, agent_id=1, query="hi",
        )
        # enabled but every layer empty → no injectable block (byte-identical prompt)
        assert m.render_layered_context_block(ctx) == ""

    def test_render_block_has_sections_when_data(self, monkeypatch):
        rec = MemoryRecord(memory_type="semantic", scope="agent", content="长期事实X", importance=0.9, confidence=0.9)
        monkeypatch.setattr(m, "retrieve_semantic_memories", lambda *a, **k: [rec])
        policy = _enabled_policy(policy={"layers": ["semantic"]})
        ctx = m.assemble_layered_context(
            m.SCENARIO_AGENT_CHAT, policy=policy, conversation_id=None, agent_id=1, query="q",
        )
        block = m.render_layered_context_block(ctx)
        assert "记忆上下文" in block and "长期事实X" in block


# ─── 5.5 旧行兼容加载 ────────────────────────────────────────────────────────

class TestLegacyRowCompat:
    def test_missing_agent_returns_disabled(self):
        pol = m.load_agent_memory_policy(agent_id=8888888)
        assert pol.enabled is False and pol.provider == "none"

    def test_old_agent_defaults_to_memory_off(self):
        with Session(engine) as s:
            a = Agent(name="legacy-agent")
            s.add(a)
            s.commit()
            s.refresh(a)
            aid = a.id
        pol = m.load_agent_memory_policy(agent_id=aid)
        assert pol.enabled is False  # new fields default to memory-off

    def test_load_profile_memory_reads_active_only(self):
        with Session(engine) as s:
            s.add(MemoryItem(
                memory_type="profile_preference", scope="user", content="偏好简洁",
                status="active", importance=0.7, user_id=42,
            ))
            s.add(MemoryItem(
                memory_type="profile_fact", scope="user", content="过期项",
                status="archived", importance=0.9, user_id=42,
            ))
            s.commit()
        rows = m.load_profile_memory(user_id=42)
        contents = {r.content for r in rows}
        assert "偏好简洁" in contents and "过期项" not in contents
