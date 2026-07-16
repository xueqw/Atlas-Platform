"""Memory service skeleton (Batch B/D).

Service-layer entry points the runtime calls to read/write layered memory. Owns
the DB Session and routes long-term reads/writes through ``memory_provider``.
This is a conservative skeleton: methods are safe no-ops / minimal queries that
default to "memory off" so the main chat / planner / replan path keeps working
when nothing is configured.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from sqlmodel import Session, select

from app.core.database import engine
from app.core.memory_provider import MemoryRecord, get_memory_provider
from app.core import session_retrieval_service as session_retrieval
from app.models.db import (
    MEMORY_JOB_STATUSES,
    MEMORY_JOB_TYPES,
    Agent,
    MemoryItem,
    MemoryLink,
    MemoryWritebackJob,
    _utcnow,
)

_log = logging.getLogger(__name__)


@dataclass
class AgentMemoryPolicy:
    """Resolved L3 capability-layer policy for one agent (optional-first)."""
    enabled: bool = False
    provider: str = "none"
    scope: str = ""
    policy: Dict[str, Any] = None
    procedural_refs: List[Any] = None
    architecture_pattern: str = ""

    @classmethod
    def disabled(cls) -> "AgentMemoryPolicy":
        return cls(enabled=False, provider="none", scope="", policy={}, procedural_refs=[], architecture_pattern="")

    def layer_enabled(self, layer: str) -> bool:
        """Whether a long-term layer participates in retrieval for this agent.

        A layer is on only when memory is enabled AND the policy opts it in. The
        opt-in shape is tolerant: ``policy["layers"]`` may be a list of enabled
        layer names or a ``{layer: bool}`` map. When memory is enabled but no
        ``layers`` key is present, all layers default ON (policy authors opt out
        explicitly); when memory is disabled, every layer is OFF.
        """
        if not self.enabled:
            return False
        layers = (self.policy or {}).get("layers")
        if layers is None:
            return True
        if isinstance(layers, dict):
            return bool(layers.get(layer, False))
        if isinstance(layers, (list, tuple, set)):
            return layer in layers
        return True


def _loads(raw: str, default):
    try:
        val = json.loads(raw) if raw else default
        return val if val is not None else default
    except (json.JSONDecodeError, TypeError):
        return default


def load_agent_memory_policy(agent_id: int, session: Optional[Session] = None) -> AgentMemoryPolicy:
    """Read an agent's memory policy. Missing agent / fields → disabled policy
    (never raises), so old agents transparently behave as memory-off."""
    own_session = session is None
    s = session or Session(engine)
    try:
        agent = s.get(Agent, agent_id)
        if agent is None:
            return AgentMemoryPolicy.disabled()
        return AgentMemoryPolicy(
            enabled=bool(getattr(agent, "memory_enabled", False)),
            provider=getattr(agent, "memory_provider", "none") or "none",
            scope=getattr(agent, "memory_scope", "") or "",
            policy=_loads(getattr(agent, "memory_policy_json", "{}"), {}),
            procedural_refs=_loads(getattr(agent, "procedural_refs_json", "[]"), []),
            architecture_pattern=getattr(agent, "architecture_pattern", "") or "",
        )
    finally:
        if own_session:
            s.close()


def load_profile_memory(
    *, user_id: Optional[int] = None, agent_id: Optional[int] = None, limit: int = 20
) -> List[MemoryItem]:
    """Structured profile recall from PG (profile_fact / profile_preference),
    active only. Pure PG read — does not touch mem0."""
    with Session(engine) as s:
        stmt = select(MemoryItem).where(
            MemoryItem.status == "active",
            MemoryItem.memory_type.in_(["profile_fact", "profile_preference"]),
        )
        if user_id is not None:
            stmt = stmt.where(MemoryItem.user_id == user_id)
        if agent_id is not None:
            stmt = stmt.where(MemoryItem.agent_id == agent_id)
        stmt = stmt.order_by(MemoryItem.importance.desc()).limit(limit)
        return list(s.exec(stmt).all())


def retrieve_semantic_memories(
    query: str,
    *,
    scope: Optional[str] = None,
    agent_id: Optional[int] = None,
    limit: int = 10,
) -> List[MemoryRecord]:
    """Semantic recall through the provider. Returns [] when provider is none /
    mem0 unavailable (provider handles fallback internally)."""
    provider = get_memory_provider()
    try:
        results = provider.retrieve_memories(
            query, scope=scope, memory_type="semantic", agent_id=agent_id, limit=limit
        )
    except Exception as exc:  # provider must never break the caller
        _log.warning("retrieve_semantic_memories failed (%s); returning empty", exc)
        results = []
    # Observability (T9): record the recall. Best-effort.
    try:
        from app.core.memory_events import record_memory_event
        record_memory_event(
            provider=getattr(provider, "name", "none"), event_type="recall",
            status="success", scope=scope or "",
            source="semantic_recall", query_or_reason=(query or "")[:500],
            payload_summary=f"召回 {len(results)} 条",
            details={"count": len(results), "limit": limit},
        )
    except Exception:
        pass
    return results


# Content classes that MUST stay in L1 session history and MUST NOT be promoted
# to long-term memory_items (spec: layered-memory-runtime §"长期记忆写入准入闸门"
# / 记忆文档 §9). Used by is_admissible_for_long_term().
_NON_LONGTERM_MESSAGE_TYPES = frozenset({"tool"})  # raw tool output
# A streamed token fragment (single delta) carries no standalone long-term value.
_STREAM_TOKEN_KINDS = frozenset({"stream_token", "token", "delta"})
# Above this length a candidate is almost certainly a raw/near-full transcript
# dump rather than a distilled fact; defer it to L1 (a summary job compresses it
# later). Generous so genuine multi-sentence facts still pass.
_FULL_TRANSCRIPT_CHAR_LIMIT = 4000

# A bare one-off file path / artifact id with no surrounding prose. We reject
# content that is *only* such a token (path-like or id-like, no whitespace).
_ARTIFACT_ID_RE = re.compile(r"^[A-Za-z0-9_.:/\\-]+$")
# Common greeting / pleasantry openers (zh + en). Matched only when the whole
# (short) content is a greeting — never as a substring of a longer statement.
_GREETING_RE = re.compile(
    r"^(你好|您好|哈喽|嗨|在吗|谢谢|多谢|感谢|不客气|没事|好的|收到|ok|okay|hi|hello|hey|"
    r"thanks|thank you|thx|bye|再见|拜拜)[!！。.~\s]*$",
    re.IGNORECASE,
)


def is_admissible_for_long_term(
    *, content: str, message_type: str = "", source_kind: str = "manual"
) -> bool:
    """Write-admission gate: decide whether a piece of content may be extracted
    into a long-term ``memory_item`` at all.

    Rejects the content classes the contract names as L1-only (spec:
    layered-memory-runtime §"长期记忆写入准入闸门"): empty/blank, raw tool output,
    streaming token fragments, bare one-off file paths / artifact ids, plain
    greetings, and full raw transcripts. Judgement is a cheap synchronous rule
    (length / pattern / message_type / source_kind) — never an LLM call, since
    this sits on a hot path.

    Stays conservative: only the clearly-disallowed classes are blocked. Richer
    salience scoring (deciding *which* admissible content is worth keeping) is
    the extractor / worker's job — this gate would rather let a borderline fact
    through than risk dropping a valuable one.
    """
    if not content or not content.strip():
        return False
    text = content.strip()
    # Raw tool output / streamed token fragments are never long-term.
    if message_type in _NON_LONGTERM_MESSAGE_TYPES:
        return False
    if message_type in _STREAM_TOKEN_KINDS:
        return False
    # Full raw transcript dump → leave in L1, a summary job compresses it.
    if len(text) > _FULL_TRANSCRIPT_CHAR_LIMIT:
        return False
    # Bare one-off artifact id / file path with no prose.
    if _ARTIFACT_ID_RE.match(text) and (
        "/" in text or "\\" in text or "." in text or "_" in text or len(text) >= 12
    ):
        return False
    # Plain greeting / pleasantry (only when the whole short message is one).
    if len(text) <= 24 and _GREETING_RE.match(text):
        return False
    return True


def queue_writeback_job(
    *, source_kind: str, source_ref: str, job_type: str, payload: Optional[Dict[str, Any]] = None
) -> Optional[MemoryWritebackJob]:
    """Idempotently enqueue a writeback job.

    De-dupes on (source_kind, source_ref, job_type) against any non-terminal
    (pending/running) job so the same source is not extracted twice. Returns the
    existing or newly-created job, or None when job_type is invalid.
    """
    if job_type not in MEMORY_JOB_TYPES:
        _log.warning("queue_writeback_job: invalid job_type=%r ignored", job_type)
        return None
    with Session(engine) as s:
        existing = s.exec(
            select(MemoryWritebackJob).where(
                MemoryWritebackJob.source_kind == source_kind,
                MemoryWritebackJob.source_ref == source_ref,
                MemoryWritebackJob.job_type == job_type,
                MemoryWritebackJob.status.in_(["pending", "running"]),
            )
        ).first()
        if existing is not None:
            return existing
        job = MemoryWritebackJob(
            source_kind=source_kind,
            source_ref=source_ref,
            job_type=job_type,
            status="pending",
            payload_json=json.dumps(payload or {}, ensure_ascii=False),
        )
        s.add(job)
        s.commit()
        s.refresh(job)
        return job


def merge_memory_candidates(candidates: List[MemoryRecord], limit: int = 10) -> List[MemoryRecord]:
    """Dedupe + rank a merged candidate list before prompt injection.

    Dedupe key is (memory_type, normalized content); on collision keep the
    higher importance/confidence representation. Result is sorted by
    importance descending and capped at ``limit`` (spec: Retrieval merge §D6).
    Recency weighting is folded in by the caller's ordering when available.
    """
    best: Dict[tuple, MemoryRecord] = {}
    for rec in candidates:
        key = (rec.memory_type, " ".join((rec.content or "").lower().split()))
        prev = best.get(key)
        if prev is None or (rec.importance, rec.confidence) > (prev.importance, prev.confidence):
            best[key] = rec
    ranked = sorted(best.values(), key=lambda r: (r.importance, r.confidence), reverse=True)
    return ranked[:limit]


# ── Layered retrieval assembly (task 2.2) ──────────────────────────────────
# A single ordered, policy-gated entry point the runtime calls to build context.
# It only *orchestrates* the existing primitives above (policy / profile /
# semantic / session reads) in the order the layered-memory contract defines —
# it does NOT render a prompt string (callers format per scenario) and does NOT
# write anything back (planner stage must never trigger long-term writes).

# Hard cap on episodic injection for the agent-chat scenario (spec:
# layered-memory-runtime §"Agent chat episodic 注入限量"). Even if a policy
# requests more, agent chat injects at most this many episodic records.
_AGENT_CHAT_EPISODIC_LIMIT = 3

# Valid retrieval scenarios.
SCENARIO_PLANNER = "planner"
SCENARIO_AGENT_CHAT = "agent_chat"
SCENARIO_REPLAN = "replan"
_SCENARIOS = frozenset({SCENARIO_PLANNER, SCENARIO_AGENT_CHAT, SCENARIO_REPLAN})


@dataclass
class LayeredContext:
    """Structured result of one layered retrieval pass.

    Each field is a layer the contract defines; callers render only the layers
    they need. ``hit_counts`` records how many items each layer contributed (for
    tests / observability). An ``empty`` instance means "no memory" — the caller
    should fall back to its pre-memory path verbatim.
    """
    scenario: str = ""
    enabled: bool = False
    # L1 session history
    recent_messages: List[Any] = None
    session_summary: str = ""
    planner_history: Any = None
    # L3 agent capability
    agent_policy: Optional["AgentMemoryPolicy"] = None
    # L2 profile
    profile: List[MemoryItem] = None
    # L5 semantic (merged + ranked records)
    semantic: List[MemoryRecord] = None
    # L4 episodic (limited)
    episodic: List[MemoryRecord] = None
    # L6 procedural refs (from policy; this change does not re-resolve skills)
    procedural_refs: List[Any] = None
    # L7 summary (optional high-density summary text)
    summary: str = ""
    hit_counts: Dict[str, int] = None

    @classmethod
    def empty(cls, scenario: str = "") -> "LayeredContext":
        return cls(
            scenario=scenario, enabled=False, recent_messages=[], session_summary="",
            planner_history=None, agent_policy=None, profile=[], semantic=[],
            episodic=[], procedural_refs=[], summary="", hit_counts={},
        )


def assemble_layered_context(
    scenario: str,
    *,
    agent_id: Optional[int] = None,
    conversation_id: Optional[int] = None,
    planner_conversation_id: Optional[str] = None,
    user_id: Optional[int] = None,
    query: str = "",
    policy: Optional["AgentMemoryPolicy"] = None,
    on_layer: Optional[Any] = None,
) -> LayeredContext:
    """Ordered, policy-gated layered retrieval entry point (spec:
    layered-memory-runtime §"受 policy 约束的分层检索装配入口").

    Resolves the agent's memory policy (unless one is passed in), then routes to
    the per-scenario assembler which reads each *enabled* layer in the contract's
    order. A layer the policy disables is never queried (not queried-then-dropped
    — saves cost and satisfies the contract). When memory is disabled the result
    is ``LayeredContext.empty`` so the caller takes its no-memory path.

    ``on_layer`` (planner-granular-step-hooks) is an optional SYNC callback
    ``on_layer(layer: str, phase: str, hit_count: int)`` invoked per real layer so
    recall is observable step-by-step. When omitted, behaviour/return is unchanged.

    Only orchestrates existing primitives; performs no long-term writeback.
    """
    if scenario not in _SCENARIOS:
        raise ValueError(f"unknown retrieval scenario: {scenario!r}")

    if policy is None and agent_id is not None:
        policy = load_agent_memory_policy(agent_id)
    if policy is None:
        policy = AgentMemoryPolicy.disabled()

    if not policy.enabled:
        # Even with memory off, surface a single observable signal so the planner
        # process shows the recall step resolving ("无长期记忆") rather than a gap.
        if on_layer is not None:
            try:
                on_layer("disabled", "done", 0)
            except Exception:
                pass
        return LayeredContext.empty(scenario)

    if scenario == SCENARIO_AGENT_CHAT:
        return _assemble_agent_chat(
            policy=policy, agent_id=agent_id, conversation_id=conversation_id,
            user_id=user_id, query=query,
        )
    if scenario == SCENARIO_PLANNER:
        return _assemble_planner(
            policy=policy, agent_id=agent_id,
            planner_conversation_id=planner_conversation_id, user_id=user_id, query=query,
            on_layer=on_layer,
        )
    return _assemble_replan(
        policy=policy, agent_id=agent_id,
        planner_conversation_id=planner_conversation_id, user_id=user_id, query=query,
    )


def _assemble_agent_chat(
    *, policy: "AgentMemoryPolicy", agent_id: Optional[int],
    conversation_id: Optional[int], user_id: Optional[int], query: str,
) -> LayeredContext:
    """Agent-chat order: recent messages → conversation summary → agent
    capability/policy → user profile → semantic → limited episodic (≤3) →
    procedural refs → summary (spec order)."""
    ctx = LayeredContext.empty(SCENARIO_AGENT_CHAT)
    ctx.enabled = True
    ctx.agent_policy = policy
    counts: Dict[str, int] = {}

    if conversation_id is not None and policy.layer_enabled("session_history"):
        ctx.recent_messages = session_retrieval.get_recent_messages(conversation_id)
        ctx.session_summary = session_retrieval.summarize_session(conversation_id)
        counts["recent_messages"] = len(ctx.recent_messages)

    if policy.layer_enabled("profile"):
        ctx.profile = load_profile_memory(user_id=user_id, agent_id=agent_id)
        counts["profile"] = len(ctx.profile)

    if policy.layer_enabled("semantic"):
        ctx.semantic = merge_memory_candidates(
            retrieve_semantic_memories(query, scope=policy.scope or None, agent_id=agent_id)
        )
        counts["semantic"] = len(ctx.semantic)

    if policy.layer_enabled("episodic"):
        episodic = retrieve_semantic_memories(
            query, scope=policy.scope or None, agent_id=agent_id, limit=_AGENT_CHAT_EPISODIC_LIMIT,
        )
        # Hard cap regardless of policy-configured size.
        ctx.episodic = episodic[:_AGENT_CHAT_EPISODIC_LIMIT]
        counts["episodic"] = len(ctx.episodic)

    if policy.layer_enabled("procedural"):
        ctx.procedural_refs = list(policy.procedural_refs or [])
        counts["procedural_refs"] = len(ctx.procedural_refs)

    ctx.hit_counts = counts
    return ctx


def _assemble_planner(
    *, policy: "AgentMemoryPolicy", agent_id: Optional[int],
    planner_conversation_id: Optional[str], user_id: Optional[int], query: str,
    on_layer: Optional[Any] = None,
) -> LayeredContext:
    """Planner order: planner working state / session history → planner session
    summary → user profile → procedural (mounted skills) → recent episodic →
    recent summary. Planner MUST NOT trigger writeback (read-only here).

    ``on_layer(layer, phase, hit_count)`` (planner-granular-step-hooks) makes each
    real recall layer observable. Best-effort; hook errors never break recall."""
    ctx = LayeredContext.empty(SCENARIO_PLANNER)
    ctx.enabled = True
    ctx.agent_policy = policy
    counts: Dict[str, int] = {}

    def _emit(layer: str, phase: str, hits: int = 0):
        if on_layer is not None:
            try:
                on_layer(layer, phase, hits)
            except Exception:
                pass

    if planner_conversation_id and policy.layer_enabled("session_history"):
        _emit("session_history", "start")
        ctx.planner_history = session_retrieval.get_planner_history(planner_conversation_id)
        counts["planner_history"] = 1 if ctx.planner_history is not None else 0
        _emit("session_history", "done", counts["planner_history"])

    if policy.layer_enabled("profile"):
        _emit("profile", "start")
        ctx.profile = load_profile_memory(user_id=user_id, agent_id=agent_id)
        counts["profile"] = len(ctx.profile)
        _emit("profile", "done", counts["profile"])

    if policy.layer_enabled("procedural"):
        _emit("procedural", "start")
        ctx.procedural_refs = list(policy.procedural_refs or [])
        counts["procedural_refs"] = len(ctx.procedural_refs)
        _emit("procedural", "done", counts["procedural_refs"])

    if policy.layer_enabled("episodic"):
        _emit("episodic", "start")
        ctx.episodic = merge_memory_candidates(
            retrieve_semantic_memories(query, scope=policy.scope or None, agent_id=agent_id)
        )
        counts["episodic"] = len(ctx.episodic)
        _emit("episodic", "done", counts["episodic"])

    ctx.hit_counts = counts
    return ctx


def _assemble_replan(
    *, policy: "AgentMemoryPolicy", agent_id: Optional[int],
    planner_conversation_id: Optional[str], user_id: Optional[int], query: str,
) -> LayeredContext:
    """Replan order: existing agent config (capability) → planner history
    summary → run/eval evidence (episodic) → user profile / confirmed decisions
    → memory policy history → relevant procedural. Read-only assembly."""
    ctx = LayeredContext.empty(SCENARIO_REPLAN)
    ctx.enabled = True
    ctx.agent_policy = policy  # L3 capability is always the replan baseline
    counts: Dict[str, int] = {}

    if planner_conversation_id and policy.layer_enabled("session_history"):
        ctx.planner_history = session_retrieval.get_planner_history(planner_conversation_id)
        counts["planner_history"] = 1 if ctx.planner_history is not None else 0

    if policy.layer_enabled("episodic"):
        ctx.episodic = merge_memory_candidates(
            retrieve_semantic_memories(query, scope=policy.scope or None, agent_id=agent_id)
        )
        counts["episodic"] = len(ctx.episodic)

    if policy.layer_enabled("profile"):
        ctx.profile = load_profile_memory(user_id=user_id, agent_id=agent_id)
        counts["profile"] = len(ctx.profile)

    if policy.layer_enabled("procedural"):
        ctx.procedural_refs = list(policy.procedural_refs or [])
        counts["procedural_refs"] = len(ctx.procedural_refs)

    ctx.hit_counts = counts
    return ctx


def render_layered_context_block(ctx: LayeredContext) -> str:
    """Render a LayeredContext into an injectable system-prompt block.

    Returns "" when memory is disabled or every layer is empty, so a caller can
    unconditionally append the result and get byte-identical behaviour to the
    pre-memory path when memory is off. Layers are rendered compactly (titles +
    bounded lists), never the raw transcript — formatting is the caller's only
    concern, ordering/limits already happened in the assembler.
    """
    if ctx is None or not ctx.enabled:
        return ""
    sections: List[str] = []

    if ctx.session_summary:
        sections.append(f"## 会话摘要\n{ctx.session_summary}")
    if ctx.profile:
        lines = [f"- {(m.summary or m.content or '').strip()}" for m in ctx.profile if (m.summary or m.content)]
        if lines:
            sections.append("## 用户画像\n" + "\n".join(lines))
    if ctx.semantic:
        lines = [f"- {(r.content or '').strip()}" for r in ctx.semantic if r.content]
        if lines:
            sections.append("## 相关长期事实\n" + "\n".join(lines))
    if ctx.episodic:
        lines = [f"- {(r.content or '').strip()}" for r in ctx.episodic if r.content]
        if lines:
            sections.append("## 相关历史事件\n" + "\n".join(lines))
    if ctx.summary:
        sections.append(f"## 历史压缩摘要\n{ctx.summary}")

    if not sections:
        return ""
    return "\n\n# 记忆上下文\n" + "\n\n".join(sections)


# ── Long-term write path: persistence + dedupe/conflict (Batch D, task 5.7) ──
# record_memory_item is the single write entry. It gates on the admission rule,
# routes the body through the provider (pgonly → mem0_ref None, body in PG), and
# resolves against existing active rows in the same (scope, memory_type, owner)
# space: duplicate / refine / supersede / conflict. Superseded/decayed rows are
# never physically deleted — only their status changes (auditable).

# Profile preference "key" is the text before the first ：/: separator, so
# "语言：中文" and "语言：英文" are recognised as the same preference with
# different values (→ supersede), while unrelated prefs don't collide.
def _pref_key(content: str) -> str:
    text = (content or "").strip()
    for sep in ("：", ":"):
        if sep in text:
            return text.split(sep, 1)[0].strip().lower()
    return _normalize(text)


def _normalize(content: str) -> str:
    return " ".join((content or "").lower().split())


def _classify_against(record: MemoryRecord, existing: List[MemoryItem]) -> tuple:
    """Return (action, row) for the first matching existing row.

    action ∈ {duplicate, refine, supersede, conflict, new}. Heuristics are
    deliberately conservative and split by memory_type (design D1)."""
    norm_new = _normalize(record.content)
    for row in existing:
        norm_old = _normalize(row.content)
        if norm_new and norm_new == norm_old:
            return ("duplicate", row)
        # profile_preference: same key, different value → supersede.
        if record.memory_type == "profile_preference":
            if _pref_key(record.content) and _pref_key(record.content) == _pref_key(row.content):
                return ("supersede", row)
        # semantic / profile_fact: new strictly contains old (more specific), or
        # is a longer statement with higher confidence → refine the old row.
        if record.memory_type in ("semantic", "profile_fact", "agent_constraint"):
            if norm_old and norm_old in norm_new and norm_old != norm_new:
                return ("refine", row)
    return ("new", None)


def _owner_filter(stmt, record: MemoryRecord):
    """Scope the existing-row query to the same owner dimension as the record."""
    stmt = stmt.where(
        MemoryItem.status == "active",
        MemoryItem.memory_type == record.memory_type,
        MemoryItem.scope == record.scope,
    )
    if record.agent_id is not None:
        stmt = stmt.where(MemoryItem.agent_id == record.agent_id)
    if record.user_id is not None:
        stmt = stmt.where(MemoryItem.user_id == record.user_id)
    return stmt


def record_memory_item(record: MemoryRecord, session: Optional[Session] = None) -> Optional[MemoryItem]:
    """Persist one candidate as a memory_items row, resolving conflicts.

    Returns the affected row (existing on duplicate/refine, new on
    supersede/conflict/new), or None when the content is inadmissible. The body
    is routed through the provider so a future mem0 read-side batch can offload
    it; pgonly keeps mem0_ref None and the body in PG ``content``.
    """
    if not is_admissible_for_long_term(content=record.content, source_kind=record.source_kind):
        return None

    own = session is None
    s = session or Session(engine)
    try:
        # Episodic memories are append-only, time-stamped events: recurrence is a
        # signal (merge_semantic promotes repeated episodic to semantic), so they
        # are never deduped/superseded here. Dedupe/conflict applies only to
        # fact-like types (profile / semantic / agent_constraint).
        if record.memory_type == "episodic":
            action, row = ("new", None)
        else:
            existing = list(s.exec(_owner_filter(select(MemoryItem), record)).all())
            action, row = _classify_against(record, existing)

        provider = get_memory_provider()
        try:
            store = provider.store_profile_memory(record) if record.memory_type.startswith("profile") \
                else provider.store_summary_memory(record) if record.memory_type == "summary" \
                else provider.store_episodic_memory(record)
            mem0_ref = store.mem0_ref
        except Exception:
            store = None
            mem0_ref = None

        # Observability (T9): record the write attempt. Best-effort — a logging
        # failure must never break the write path. status reflects whether the
        # body reached mem0 (mem0_ref set) vs degraded to pgonly.
        try:
            from app.core.memory_events import record_memory_event
            _wstatus = "success" if (store and getattr(store, "ok", False)) else "failed"
            if store and getattr(store, "provider", "") == "pgonly" and mem0_ref is None:
                _wstatus = "degraded"
            record_memory_event(
                provider=(getattr(store, "provider", None) or "none"),
                event_type="write", status=_wstatus, scope=record.scope,
                source=record.source_kind, source_ref=record.source_ref,
                query_or_reason=f"write {record.memory_type}",
                payload_summary=(record.summary or record.content or "")[:300],
                details={"memory_type": record.memory_type, "mem0_ref": mem0_ref},
            )
        except Exception:
            pass

        if action == "duplicate":
            row.last_accessed_at = _utcnow()
            row.access_count = (row.access_count or 0) + 1
            s.add(row); s.commit(); s.refresh(row)
            return row
        if action == "refine":
            row.content = record.content.strip()
            row.importance = max(row.importance or 0.0, record.importance or 0.0)
            row.confidence = max(row.confidence or 0.0, record.confidence or 0.0)
            row.updated_at = _utcnow()
            s.add(row); s.commit(); s.refresh(row)
            return row

        new_row = MemoryItem(
            agent_id=record.agent_id, user_id=record.user_id,
            memory_type=record.memory_type, scope=record.scope,
            content=record.content.strip(), summary=record.summary or "",
            importance=record.importance or 0.0, confidence=record.confidence or 0.0,
            status="active", source_kind=record.source_kind, source_ref=record.source_ref or "",
            mem0_ref=mem0_ref,
        )
        if action == "conflict" and row is not None:
            row.importance = (row.importance or 0.0) * 0.5
            new_row.importance = max(new_row.importance, (row.importance or 0.0) + 0.01)
            s.add(row)
        s.add(new_row); s.commit(); s.refresh(new_row)

        if action == "supersede" and row is not None:
            row.status = "superseded"
            row.superseded_by = new_row.id
            row.updated_at = _utcnow()
            s.add(row)
            s.add(MemoryLink(from_memory_id=new_row.id, to_memory_id=row.id, link_type="supersedes"))
            s.commit(); s.refresh(new_row)
        return new_row
    finally:
        if own:
            s.close()


# ── Compression + decay (Batch D, task 5.8) ─────────────────────────────────
# Thresholds are module constants so they're easy to tune/test.
EPISODIC_COMPRESS_COUNT = 20         # > this many active episodic in a scope
EPISODIC_COMPRESS_AGE_DAYS = 7       # or oldest active episodic older than this
EPISODIC_DECAY_DAYS = 30             # stale low-importance episodic → archived
EPISODIC_DECAY_IMPORTANCE = 0.3      # "low importance" threshold


def _scope_episodic(s: Session, scope: str, agent_id: Optional[int]) -> List[MemoryItem]:
    stmt = select(MemoryItem).where(
        MemoryItem.status == "active", MemoryItem.memory_type == "episodic", MemoryItem.scope == scope,
    )
    if agent_id is not None:
        stmt = stmt.where(MemoryItem.agent_id == agent_id)
    return list(s.exec(stmt.order_by(MemoryItem.created_at)).all())


def compress_episodic_to_summary(
    scope: str, agent_id: Optional[int] = None, *, force: bool = False, session: Optional[Session] = None
) -> Optional[MemoryItem]:
    """Compress a scope's active episodic into one summary when over threshold.

    Builds a summary row, links it to each compressed episodic via
    ``memory_links(summarizes)``, and archives those episodic (never deletes).
    Returns the summary row, or None when below threshold and not forced.
    """
    from datetime import timedelta

    own = session is None
    s = session or Session(engine)
    try:
        eps = _scope_episodic(s, scope, agent_id)
        if not eps:
            return None
        oldest_age = _utcnow() - (eps[0].created_at or _utcnow())
        over = (
            force
            or len(eps) > EPISODIC_COMPRESS_COUNT
            or oldest_age > timedelta(days=EPISODIC_COMPRESS_AGE_DAYS)
        )
        if not over:
            return None
        body = "\n".join(f"- {(e.summary or e.content or '').strip()}" for e in eps)[:2000]
        summary = MemoryItem(
            agent_id=agent_id, memory_type="summary", scope=scope,
            content=body, summary=body[:500], importance=0.5, confidence=0.5,
            status="active", source_kind="run_summary", source_ref=f"compress:{scope}",
        )
        s.add(summary); s.commit(); s.refresh(summary)
        for e in eps:
            e.status = "archived"; e.updated_at = _utcnow(); s.add(e)
            s.add(MemoryLink(from_memory_id=summary.id, to_memory_id=e.id, link_type="summarizes"))
        s.commit(); s.refresh(summary)
        return summary
    finally:
        if own:
            s.close()


def decay_stale_episodic(session: Optional[Session] = None) -> int:
    """Archive low-importance episodic untouched for EPISODIC_DECAY_DAYS. Returns count."""
    from datetime import timedelta

    own = session is None
    s = session or Session(engine)
    try:
        cutoff = _utcnow() - timedelta(days=EPISODIC_DECAY_DAYS)
        rows = list(s.exec(
            select(MemoryItem).where(
                MemoryItem.status == "active",
                MemoryItem.memory_type == "episodic",
                MemoryItem.importance < EPISODIC_DECAY_IMPORTANCE,
            )
        ).all())
        n = 0
        for r in rows:
            seen = r.last_accessed_at or r.created_at
            if seen is not None and seen < cutoff:
                r.status = "archived"; r.updated_at = _utcnow(); s.add(r); n += 1
        if n:
            s.commit()
        return n
    finally:
        if own:
            s.close()


def enqueue_writeback_if_enabled(
    *, agent_id: Optional[int], job_type: str, source_kind: str, source_ref: str, payload: Optional[dict] = None
) -> None:
    """Best-effort writeback enqueue, gated on the agent's memory policy.

    No-ops (and never raises) when memory is disabled / no agent_id, so the main
    chat / planner / apply path is byte-identical when memory is off. Failures
    are logged and swallowed — writeback must never break the caller (design D4).
    """
    try:
        if agent_id is None:
            return
        policy = load_agent_memory_policy(agent_id)
        if not policy.enabled:
            return
        from app.core import job_queue
        job_queue.enqueue(job_type=job_type, source_kind=source_kind, source_ref=source_ref, payload=payload)
    except Exception as exc:  # writeback enqueue must not break the main path
        _log.warning("enqueue_writeback_if_enabled(%s) failed: %s", job_type, exc)


