"""Tests for the layered-memory + infra-decoupling foundation (Batch B/C/D skeleton).

Covers:
  1. DATABASE_URL resolution: dev fallback + production guard.
  2. Agent / Message new-field defaults (optional-first, memory off).
  3. memory_provider fallback when mem0 is unconfigured / unavailable.
  4. apply path landing memory policy onto the Agent row.

These exercise the conservative defaults: with nothing configured the system
behaves exactly as before (memory off, SQLite dev fallback).
"""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.api import planner as planner_api
from app.main import app
from app.models.db import (
    Agent,
    Message,
    Conversation,
    MemoryItem,
    MemoryWritebackJob,
)


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


# --- 1. DATABASE_URL resolution + production guard ---------------------------

class TestDatabaseUrlResolution:
    def _resolve(self, monkeypatch, **env):
        import app.core.database as db
        for k in ("DATABASE_URL", "APP_ENV"):
            monkeypatch.delenv(k, raising=False)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        return db._resolve_database_url()

    def test_dev_fallback_to_sqlite_when_unset(self, monkeypatch):
        url = self._resolve(monkeypatch)
        assert url.startswith("sqlite:///")

    def test_dev_honors_explicit_postgres_url(self, monkeypatch):
        url = self._resolve(monkeypatch, DATABASE_URL="postgresql+psycopg://u:p@h:5432/d")
        assert url == "postgresql+psycopg://u:p@h:5432/d"

    def test_production_without_url_is_error(self, monkeypatch):
        with pytest.raises(RuntimeError):
            self._resolve(monkeypatch, APP_ENV="production")

    def test_production_with_sqlite_is_error(self, monkeypatch):
        with pytest.raises(RuntimeError):
            self._resolve(monkeypatch, APP_ENV="production", DATABASE_URL="sqlite:///x.db")

    def test_production_with_postgres_ok(self, monkeypatch):
        url = self._resolve(
            monkeypatch, APP_ENV="production", DATABASE_URL="postgresql+psycopg://u:p@h:5432/d"
        )
        assert url.startswith("postgresql")


# --- 2. New-field defaults (optional-first) ----------------------------------

class TestNewFieldDefaults:
    def test_agent_memory_fields_default_off(self):
        with Session(planner_api.engine) as s:
            agent = Agent(name="defaults-agent")
            s.add(agent)
            s.commit()
            s.refresh(agent)
            assert agent.memory_enabled is False
            assert agent.memory_provider == "none"
            assert agent.memory_scope == ""
            assert agent.memory_policy_json == "{}"
            assert agent.procedural_refs_json == "[]"
            assert agent.architecture_pattern == ""

    def test_message_metadata_fields_default_empty(self):
        with Session(planner_api.engine) as s:
            conv = Conversation(agent_id=1, title="t")
            s.add(conv)
            s.commit()
            s.refresh(conv)
            msg = Message(conversation_id=conv.id, role="user", content="hi")
            s.add(msg)
            s.commit()
            s.refresh(msg)
            assert msg.message_type == ""
            assert msg.session_kind == ""
            assert msg.metadata_json == "{}"
            assert msg.importance_score == 0.0
            assert msg.summary_status == ""
            assert msg.embedding_status == ""

    def test_memory_item_defaults(self):
        with Session(planner_api.engine) as s:
            item = MemoryItem(content="user prefers dark mode")
            s.add(item)
            s.commit()
            s.refresh(item)
            assert item.memory_type == "semantic"
            assert item.scope == "agent"
            assert item.status == "active"
            assert item.source_kind == "manual"
            assert item.mem0_ref is None
            assert item.user_id is None  # single-user phase writes null


# --- 3. memory_provider fallback ---------------------------------------------

class TestMemoryProviderFallback:
    def _build(self, monkeypatch, **env):
        import app.core.memory_provider as mp
        for k in ("MEMORY_PROVIDER", "ENABLE_MEM0", "MEM0_BASE_URL", "MEM0_API_KEY"):
            monkeypatch.delenv(k, raising=False)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        return mp.build_memory_provider()

    def test_default_is_none_provider(self, monkeypatch):
        p = self._build(monkeypatch)
        assert p.name == "none"
        assert p.retrieve_memories("anything") == []

    def test_pgonly_selected(self, monkeypatch):
        p = self._build(monkeypatch, MEMORY_PROVIDER="pgonly")
        assert p.name == "pgonly"

    def test_mem0_without_enable_flag_falls_back_to_pgonly(self, monkeypatch):
        # MEMORY_PROVIDER=mem0 but ENABLE_MEM0 unset → degrade to pgonly.
        p = self._build(monkeypatch, MEMORY_PROVIDER="mem0", MEM0_BASE_URL="http://127.0.0.1:0")
        assert p.name == "pgonly"

    def test_mem0_enabled_but_unreachable_falls_back_to_pgonly(self, monkeypatch):
        # Health check against an unroutable URL fails → pgonly, no exception.
        p = self._build(
            monkeypatch,
            MEMORY_PROVIDER="mem0",
            ENABLE_MEM0="true",
            MEM0_BASE_URL="http://127.0.0.1:1",  # nothing listens here
        )
        assert p.name == "pgonly"

    def test_mem0_enabled_without_base_url_falls_back(self, monkeypatch):
        p = self._build(monkeypatch, MEMORY_PROVIDER="mem0", ENABLE_MEM0="true")
        assert p.name == "pgonly"

    def test_unknown_provider_defaults_to_none(self, monkeypatch):
        p = self._build(monkeypatch, MEMORY_PROVIDER="bogus")
        assert p.name == "none"

    def test_health_probe_swallows_connection_errors(self, monkeypatch):
        # A broken mem0 (e.g. ConnectionResetError, like the real JWT_SECRET-crash
        # container) must not raise out of health_check — it returns False so the
        # caller degrades. Simulate the reset without needing the live service.
        import app.core.memory_provider as mp

        def _boom(*a, **k):
            raise ConnectionResetError(104, "Connection reset by peer")

        monkeypatch.setattr(mp.urllib.request, "urlopen", _boom)
        prov = mp.Mem0MemoryProvider(base_url="http://127.0.0.1:8888")
        assert prov.health_check() is False
        # retrieve degrades to the pgonly fallback (empty), never propagates.
        assert prov.retrieve_memories("anything") == []


# --- 3b. when-to-read-mem0 decision (task 5.4) -------------------------------

class TestMem0ReadDecision:
    def _set(self, monkeypatch, **env):
        import app.core.memory_provider as mp
        for k in ("MEMORY_PROVIDER", "ENABLE_MEM0", "MEM0_BASE_URL", "MEM0_API_KEY"):
            monkeypatch.delenv(k, raising=False)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        return mp

    def test_non_semantic_kinds_never_read_mem0(self, monkeypatch):
        # Even with mem0 fully enabled+healthy, working state / transcript /
        # config are non-semantic and MUST NOT consult mem0.
        mp = self._set(monkeypatch, MEMORY_PROVIDER="mem0", ENABLE_MEM0="true",
                       MEM0_BASE_URL="http://127.0.0.1:8888")
        healthy = mp.Mem0MemoryProvider(base_url="http://h")
        monkeypatch.setattr(healthy, "health_check", lambda: True)
        for kind in ("working_state", "session_transcript", "agent_config", "", None):
            assert mp.should_read_mem0(kind, provider=healthy) is False

    def test_semantic_kinds_recognized(self, monkeypatch):
        mp = self._set(monkeypatch)
        for kind in ("semantic", "episodic", "summary", "profile", "PROFILE"):
            assert mp.is_semantic_retrieval_kind(kind) is True
        assert mp.is_semantic_retrieval_kind("working_state") is False

    def test_semantic_read_blocked_when_disabled(self, monkeypatch):
        # Semantic kind but ENABLE_MEM0 unset → don't read mem0.
        mp = self._set(monkeypatch, MEMORY_PROVIDER="mem0",
                       MEM0_BASE_URL="http://127.0.0.1:8888")
        healthy = mp.Mem0MemoryProvider(base_url="http://h")
        monkeypatch.setattr(healthy, "health_check", lambda: True)
        assert mp.should_read_mem0("semantic", provider=healthy) is False

    def test_semantic_read_blocked_when_provider_degraded(self, monkeypatch):
        # mem0 enabled but the effective provider already fell back to pgonly
        # (the real situation: JWT_SECRET-crashed mem0) → don't read mem0.
        mp = self._set(monkeypatch, MEMORY_PROVIDER="mem0", ENABLE_MEM0="true",
                       MEM0_BASE_URL="http://127.0.0.1:8888")
        assert mp.should_read_mem0("semantic", provider=mp.PgOnlyMemoryProvider()) is False

    def test_semantic_read_allowed_when_healthy(self, monkeypatch):
        mp = self._set(monkeypatch, MEMORY_PROVIDER="mem0", ENABLE_MEM0="true",
                       MEM0_BASE_URL="http://127.0.0.1:8888")
        healthy = mp.Mem0MemoryProvider(base_url="http://h")
        monkeypatch.setattr(healthy, "health_check", lambda: True)
        assert mp.should_read_mem0("semantic", provider=healthy) is True

    def test_semantic_read_blocked_when_health_fails(self, monkeypatch):
        # Live mem0 adapter but health probe fails at read time → don't read.
        mp = self._set(monkeypatch, MEMORY_PROVIDER="mem0", ENABLE_MEM0="true",
                       MEM0_BASE_URL="http://127.0.0.1:8888")
        broken = mp.Mem0MemoryProvider(base_url="http://h")
        monkeypatch.setattr(broken, "health_check", lambda: False)
        assert mp.should_read_mem0("semantic", provider=broken) is False


# --- 4. apply path lands memory policy ---------------------------------------

def _baseline_proposal() -> dict:
    return {
        "architecture_summary": "mem policy agent",
        "nodes": [
            {
                "id": "p1",
                "type": "p",
                "config": {
                    "role_name": "r",
                    "system_prompt": "你是助手。",
                    "output_format": "markdown",
                },
            },
            {
                "id": "agent1",
                "type": "agent",
                "config": {
                    "role_name": "r",
                    "system_prompt": "你是助手。",
                    "model_name": "qwen3.6-27b",
                    "provider": "glm",
                    "temperature": 0.3,
                    "max_tokens": 1024,
                },
            },
        ],
        "edges": [{"source": "p1", "target": "agent1", "targetHandle": "prompt"}],
        "rationale": "r",
    }


class TestApplyLandsMemoryPolicy:
    def test_apply_without_memory_block_keeps_memory_off(self, client):
        body = {"proposal": _baseline_proposal(), "agent_name": "no-mem-agent", "memory": {}}
        res = client.post("/api/planner/apply", json=body)
        assert res.status_code == 200, res.text
        agent_id = res.json()["id"]
        with Session(planner_api.engine) as s:
            agent = s.get(Agent, agent_id)
            assert agent.memory_enabled is False
            assert agent.memory_provider == "none"
            assert agent.memory_policy_json == "{}"

    def test_apply_with_memory_block_lands_policy(self, client):
        body = {
            "proposal": _baseline_proposal(),
            "agent_name": "mem-agent",
            "memory": {
                "memory": {
                    "enabled": True,
                    "provider": "pgonly",
                    "scope": "agent_user",
                    "procedural_refs": ["skill_search"],
                    "architecture_pattern": "rag",
                }
            },
        }
        res = client.post("/api/planner/apply", json=body)
        assert res.status_code == 200, res.text
        agent_id = res.json()["id"]
        with Session(planner_api.engine) as s:
            agent = s.get(Agent, agent_id)
            assert agent.memory_enabled is True
            assert agent.memory_provider == "pgonly"
            assert agent.memory_scope == "agent_user"
            assert agent.architecture_pattern == "rag"
            assert "skill_search" in agent.procedural_refs_json
            assert agent.memory_policy_json != "{}"

    def test_apply_with_memory_in_proposal_agent_spec(self, client):
        prop = _baseline_proposal()
        prop["agent_spec"] = {"memory": {"enabled": True, "provider": "mem0", "scope": "agent"}}
        body = {"proposal": prop, "agent_name": "spec-mem-agent", "memory": {}}
        res = client.post("/api/planner/apply", json=body)
        assert res.status_code == 200, res.text
        with Session(planner_api.engine) as s:
            agent = s.get(Agent, res.json()["id"])
            assert agent.memory_enabled is True
            assert agent.memory_provider == "mem0"


# --- 5. writeback job idempotent enqueue (service skeleton) ------------------

class TestWritebackQueue:
    def test_queue_dedupes_pending_jobs(self):
        from app.core import memory_service as ms
        a = ms.queue_writeback_job(
            source_kind="conversation", source_ref="conv-xyz", job_type="extract_episodic"
        )
        b = ms.queue_writeback_job(
            source_kind="conversation", source_ref="conv-xyz", job_type="extract_episodic"
        )
        assert a is not None and b is not None
        assert a.id == b.id  # idempotent on (source_kind, source_ref, job_type)

    def test_invalid_job_type_ignored(self):
        from app.core import memory_service as ms
        assert ms.queue_writeback_job(
            source_kind="conversation", source_ref="x", job_type="bogus"
        ) is None


# --- 6. write-admission gate + extractor (tasks 2.5 / 2.6) -------------------

class TestWriteAdmission:
    def test_empty_and_tool_output_rejected(self):
        from app.core import memory_service as ms
        assert ms.is_admissible_for_long_term(content="") is False
        assert ms.is_admissible_for_long_term(content="   ") is False
        # raw tool output stays in L1 session history, never long-term
        assert ms.is_admissible_for_long_term(
            content="{...}", message_type="tool"
        ) is False

    def test_substantive_content_admissible(self):
        from app.core import memory_service as ms
        assert ms.is_admissible_for_long_term(
            content="user prefers concise answers", message_type="assistant"
        ) is True

    def test_extractor_drops_inadmissible_episodic(self):
        from app.core import memory_extractor as ext
        # empty summary → no candidate
        assert ext.extract_episodic_candidates(
            source_kind="conversation", source_ref="c1", payload={}
        ) == []
        # substantive summary → one episodic candidate
        recs = ext.extract_episodic_candidates(
            source_kind="proposal", source_ref="p1",
            payload={"summary": "switched to rag topology after eval failure"},
            agent_id=7,
        )
        assert len(recs) == 1
        assert recs[0].memory_type == "episodic"
        assert recs[0].agent_id == 7


# --- 7. retrieval merge dedupe + cap (task 5.x merge helper) -----------------

class TestRetrievalMerge:
    def test_merge_dedupes_and_keeps_higher_importance(self):
        from app.core import memory_service as ms
        from app.core.memory_provider import MemoryRecord
        cands = [
            MemoryRecord(memory_type="semantic", scope="agent",
                         content="prefers dark mode", importance=0.3, confidence=0.4),
            MemoryRecord(memory_type="semantic", scope="agent",
                         content="Prefers dark   mode", importance=0.8, confidence=0.6),
            MemoryRecord(memory_type="episodic", scope="agent",
                         content="prefers dark mode", importance=0.5, confidence=0.5),
        ]
        merged = ms.merge_memory_candidates(cands, limit=10)
        # the two semantic dupes collapse to the higher-importance one; the
        # episodic with same text is a distinct (type, content) key.
        sem = [m for m in merged if m.memory_type == "semantic"]
        assert len(sem) == 1
        assert sem[0].importance == 0.8
        assert len(merged) == 2

    def test_merge_caps_to_limit(self):
        from app.core import memory_service as ms
        from app.core.memory_provider import MemoryRecord
        cands = [
            MemoryRecord(memory_type="semantic", scope="agent",
                         content=f"fact {i}", importance=i / 10.0)
            for i in range(10)
        ]
        merged = ms.merge_memory_candidates(cands, limit=3)
        assert len(merged) == 3
        # sorted by importance desc
        assert [round(m.importance, 1) for m in merged] == [0.9, 0.8, 0.7]


# --- 8. session-history retrieval service (task 2.4) -------------------------

class TestSessionRetrieval:
    def test_recent_messages_oldest_first_and_summary(self):
        from app.core import session_retrieval_service as srs
        with Session(planner_api.engine) as s:
            conv = Conversation(agent_id=1, title="hist")
            s.add(conv)
            s.commit()
            s.refresh(conv)
            for i in range(3):
                s.add(Message(conversation_id=conv.id, role="user", content=f"m{i}"))
            s.commit()
            conv_id = conv.id
        msgs = srs.get_recent_messages(conv_id, limit=10)
        assert [m.content for m in msgs] == ["m0", "m1", "m2"]  # oldest-first
        summary = srs.summarize_session(conv_id, max_turns=10)
        assert "user: m0" in summary and "user: m2" in summary

    def test_recent_messages_empty_conversation(self):
        from app.core import session_retrieval_service as srs
        assert srs.get_recent_messages(999999) == []
        assert srs.summarize_session(999999) == ""
