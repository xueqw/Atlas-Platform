"""Pure text / JSON helpers for proposals — no DB, no app state.

Stream-tag strippers, proposal extraction, and small JSON utilities. These are
side-effect-free and depend only on the standard library.
"""

import json
import re
from typing import List, Optional


def _extract_memory_update(text: str) -> tuple:
    """Extract <memory_update> JSON from response text. Returns (clean_text, memory_dict or None)."""
    match = re.search(r"<memory_update>\s*(\{[\s\S]*?\})\s*</memory_update>", text)
    if not match:
        return text, None
    try:
        memory_data = json.loads(match.group(1))
        clean = text[:match.start()].rstrip()
        return clean, memory_data
    except json.JSONDecodeError:
        return text, None


def _try_extract_proposal(text: str) -> Optional[dict]:
    """Look for a {"ready": true, "proposal": {...}} block in assistant text.

    The model is instructed to wrap the structured proposal in a JSON code
    fence, but real outputs occasionally drop the fence. Both shapes are
    accepted; non-matching text returns None.
    """
    if not text:
        return None
    candidates: List[str] = []
    # A confirmation turn may echo one JSON memory snapshot before the actual
    # proposal. Inspect every fence in order; stopping at the first fence made
    # valid second-block proposals look like plain text and left the UI drafting.
    candidates.extend(
        block.strip()
        for block in re.findall(r"```(?:json)?\s*([\s\S]*?)```", text)
        if block.strip()
    )
    # As a fallback, try the largest brace-balanced block in the text.
    candidates.append(text.strip())
    for raw in candidates:
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, dict) and data.get("ready") and isinstance(data.get("proposal"), dict):
            return data["proposal"]
    return None


def _strip_proposal_text(text: str) -> str:
    """Return the human-readable prose of a proposal turn, with the structured
    JSON removed. Used so the persisted assistant message never carries the raw
    proposal JSON (the proposal is delivered as proposal.json instead)."""
    out = re.sub(r"```(?:json)?[\s\S]*?```", "", text or "").strip()
    # If nothing remains, or the remainder is itself the bare (un-fenced)
    # proposal JSON, there is no prose worth keeping.
    if not out or _try_extract_proposal(out) is not None:
        return ""
    return out


def _build_short_summary(proposal: dict, memory: dict) -> str:
    """Produce ≤ 800-char human-readable summary referencing files instead of dumping JSON."""
    arch = str(proposal.get("architecture_summary") or "（未提供架构摘要）")
    nodes = proposal.get("nodes") or []
    n_nodes = len(nodes) if isinstance(nodes, list) else 0
    edges = proposal.get("edges") or []
    n_edges = len(edges) if isinstance(edges, list) else 0
    classification = (memory or {}).get("task_classification") or proposal.get("task_classification") or "未分类"
    constraints = (memory or {}).get("confirmed_constraints") or []
    parts: List[str] = [
        "✅ 方案已就绪，完整内容见 `proposal.json`、`architecture.md`、`final.md`。",
        f"- **任务分型**：{classification}",
        f"- **架构**：{arch}",
        f"- **拓扑**：{n_nodes} 节点 / {n_edges} 连接",
    ]
    if constraints:
        parts.append("- **关键约束**：" + "、".join(str(c) for c in constraints[:5]))
    # Replan turns carry a diff: surface kept/added/removed counts so the chat
    # summary reflects "modification" rather than "fresh design".
    diff = proposal.get("diff")
    if isinstance(diff, dict):
        kept = diff.get("kept") or []
        added = diff.get("added") or []
        removed = diff.get("removed") or []
        parts.append(
            f"- **修订**：保留 {len(kept)} / 新增 {len(added)} / 废弃 {len(removed)} 节点"
        )
        rec = proposal.get("apply_recommendation")
        if rec:
            parts.append(f"- **落地建议**：{'建议直接应用' if rec == 'direct' else '建议先生成草案'}")
    parts.append("点击右侧 PlannerPanel 中的「创建到工作台」按钮把方案应用到智能体。")
    summary = "\n".join(parts)
    if len(summary) > 800:
        summary = summary[:797] + "…"
    return summary


_AGENT_SPEC_SUBKEYS = ("identity", "runtime", "memory", "knowledge", "evaluation", "rollout")


def _extract_agent_spec(proposal: dict) -> Optional[dict]:
    """Return the proposal's ``agent_spec`` when present and well-formed, else None.

    agent_spec is optional (freeze contract D5): old proposals carry only
    nodes/edges. We accept any dict; only the documented sub-keys are surfaced
    (unknown keys ignored, missing keys simply absent) so a partial spec never
    raises (spec: agent_spec 存在时被兼容读取 / 旧 proposal 无 agent_spec 仍可落地)."""
    if not isinstance(proposal, dict):
        return None
    spec = proposal.get("agent_spec")
    if not isinstance(spec, dict) or not spec:
        return None
    out = {k: spec[k] for k in _AGENT_SPEC_SUBKEYS if k in spec}
    return out or None


def _extract_proposal_meta(proposal: dict) -> dict:
    """Pull the optional top-level proposal meta (agent_spec / architecture_pattern
    / apply_readiness) for compat reading by apply (task 4.1/4.2). Always returns
    a dict; absent fields are omitted, never raising on old proposals."""
    proposal = proposal if isinstance(proposal, dict) else {}
    meta: dict = {}
    spec = _extract_agent_spec(proposal)
    if spec is not None:
        meta["agent_spec"] = spec
    pattern = proposal.get("architecture_pattern")
    if isinstance(pattern, str) and pattern.strip():
        meta["architecture_pattern"] = pattern.strip()
    readiness = proposal.get("apply_readiness")
    if isinstance(readiness, dict) and readiness:
        meta["apply_readiness"] = readiness
    return meta


def _proposal_meta(proposal: dict, memory: Optional[dict] = None) -> dict:
    """Derive final_summary metadata from a structured proposal (task 5.3).

    Returns ``{apply_readiness, architecture_pattern, kind}`` where:
      - apply_readiness: the proposal's apply_readiness, else memory's, else a
        safe ``{"status": "not_ready", ...}`` default.
      - architecture_pattern: proposal's, else memory's, else "".
      - kind: "ready" when apply_readiness.status == "ready"; "spec" when an
        agent_spec is present; otherwise "draft".
    All optional — old proposals (no agent_spec / readiness) still yield a
    valid, conservative meta block."""
    proposal = proposal if isinstance(proposal, dict) else {}
    memory = memory if isinstance(memory, dict) else {}

    readiness = proposal.get("apply_readiness")
    if not isinstance(readiness, dict):
        readiness = memory.get("apply_readiness")
    if not isinstance(readiness, dict):
        readiness = {"status": "not_ready", "missing": [], "recommendation": ""}

    pattern = str(
        proposal.get("architecture_pattern")
        or memory.get("architecture_pattern")
        or ""
    )

    status = str(readiness.get("status") or "not_ready")
    has_spec = isinstance(proposal.get("agent_spec"), dict)
    if status == "ready":
        kind = "ready"
    elif has_spec:
        kind = "spec"
    else:
        kind = "draft"

    return {
        "apply_readiness": readiness,
        "architecture_pattern": pattern,
        "kind": kind,
    }


class _MemoryTagStripper:
    """Streaming filter that swallows <memory_update>...</memory_update> blocks
    so they never reach the chat bubble. Tokens may split mid-tag, so we hold
    back any trailing fragment that could become a tag opener until we see
    enough characters to decide.
    """

    _OPEN = "<memory_update>"
    _CLOSE = "</memory_update>"

    def __init__(self) -> None:
        self._buffer = ""
        self._inside = False

    def feed(self, chunk: str) -> str:
        """Consume an incoming token; return the substring that is safe to emit."""
        self._buffer += chunk
        out = ""
        while True:
            if not self._inside:
                idx = self._buffer.find(self._OPEN)
                if idx == -1:
                    # No open tag yet, but the tail might be the start of one;
                    # hold back the longest suffix that could become _OPEN.
                    safe_len = len(self._buffer) - (len(self._OPEN) - 1)
                    if safe_len > 0:
                        out += self._buffer[:safe_len]
                        self._buffer = self._buffer[safe_len:]
                    return out
                # Emit text before the open tag, switch to swallowing mode.
                out += self._buffer[:idx]
                self._buffer = self._buffer[idx + len(self._OPEN):]
                self._inside = True
            else:
                idx = self._buffer.find(self._CLOSE)
                if idx == -1:
                    # Stay inside; drop everything except a possible close-tag prefix.
                    keep = min(len(self._buffer), len(self._CLOSE) - 1)
                    self._buffer = self._buffer[-keep:] if keep else ""
                    return out
                self._buffer = self._buffer[idx + len(self._CLOSE):]
                self._inside = False

    def flush(self) -> str:
        """Emit any remaining buffer that is not part of an unclosed tag."""
        if self._inside:
            return ""
        out, self._buffer = self._buffer, ""
        return out


class _A2UITagStripper(_MemoryTagStripper):
    """Same streaming-tag filter, but for `<a2ui_request>...</a2ui_request>`.

    Subclasses _MemoryTagStripper to share the buffering logic; only the
    tag pair differs. Backend parses the raw block out of the assembled
    full_response after `done`, then pushes it as a structured WS event.
    """

    _OPEN = "<a2ui_request>"
    _CLOSE = "</a2ui_request>"


class _ProposalJsonStripper:
    """Streaming filter that withholds the final ```json proposal block from the
    chat surface so it is never printed live (spec: 流式阶段不泄露 JSON).

    Unlike the memory/a2ui strippers it does NOT discard the held text itself.
    Once it sees a ```json fence it buffers everything after it into ``held`` and
    emits nothing more. The WS ``done`` handler decides: if the assembled
    response parses as a structured proposal the held text is dropped (the
    proposal is delivered as a file instead); otherwise ``held`` is emitted so a
    legitimate code block is not lost.
    """

    _FENCE = "```json"

    def __init__(self) -> None:
        self._buffer = ""
        self._inside = False
        self._held = ""

    def feed(self, chunk: str) -> str:
        if self._inside:
            self._held += chunk
            return ""
        self._buffer += chunk
        idx = self._buffer.find(self._FENCE)
        if idx == -1:
            # Hold back the longest suffix that could become the fence opener.
            safe_len = len(self._buffer) - (len(self._FENCE) - 1)
            if safe_len > 0:
                out = self._buffer[:safe_len]
                self._buffer = self._buffer[safe_len:]
                return out
            return ""
        out = self._buffer[:idx]
        self._held = self._buffer[idx:]
        self._buffer = ""
        self._inside = True
        return out

    @property
    def inside(self) -> bool:
        return self._inside

    @property
    def held(self) -> str:
        return self._held

    def flush(self) -> str:
        """Outside a fence: emit any safe remainder. Inside: emit nothing (the
        caller inspects ``held`` and decides)."""
        if self._inside:
            return ""
        out, self._buffer = self._buffer, ""
        return out


def _extract_a2ui_request(text: str) -> Optional[dict]:
    """Pull the first `<a2ui_request>` JSON block from assistant text, if any."""
    if not text:
        return None
    match = re.search(r"<a2ui_request>\s*(\{[\s\S]*?\})\s*</a2ui_request>", text)
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    options = data.get("options")
    if not isinstance(options, list) or not options:
        return None
    return data


def _parse_json_list(raw: str) -> List[str]:
    try:
        v = json.loads(raw or "[]")
        return [str(x) for x in v] if isinstance(v, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


# ── Plan-first structured validation (T2: planner-proposal-flow, design D2/D5) ─

def _validate_proposal_payload(proposal: dict):
    """Validate an extracted proposal dict against the Proposal Schema.

    Returns ``(ProposalPayload | None, ok: bool)``. ``ok`` is False on genuine
    format drift (wrong types / non-dict) so the caller can degrade (keep the
    text reply, log, don't abort the turn — spec "校验失败降级不阻断回复").
    Import is local to avoid a schemas→proposal cycle at module load."""
    from .schemas import ProposalPayload
    if not isinstance(proposal, dict):
        return None, False
    try:
        return ProposalPayload(**proposal), True
    except Exception:
        return None, False


def _match_capability_candidates(planning_ctx: Optional[dict], proposal: Optional[dict],
                                 *, expert_tags: Optional[List[str]] = None,
                                 session=None, limit: int = 8) -> List[dict]:
    """Planner-side capability match (T4; upgrades the T2 minimal heuristic).

    Scores ``internal_context.capabilities`` with a transparent additive
    heuristic (no model call):
      - capability name/tags overlapping the goal text + proposal's recommended
        names  → ``_TEXT_WEIGHT`` each
      - capability tags overlapping the matched expert template's
        ``recommended_capability_tags`` (``expert_tags``) → ``_EXPERT_TAG_WEIGHT``
        each (stronger — the expert template is a domain signal)
    Returns top-N structured refs (id/type/name) sorted by score desc; no raw
    config/description content. Returns [] when there are no capabilities.

    Capability tags are NOT in internal_context (which carries only id/type/name),
    so when a ``session`` is given we batch-read ``CapabilityItem.tags`` by id;
    without a session the matcher degrades to name-only (backward compatible with
    T2 callers/tests)."""
    ctx = planning_ctx if isinstance(planning_ctx, dict) else {}
    caps = ((ctx.get("internal_context") or {}).get("capabilities")) or []
    if not isinstance(caps, list) or not caps:
        return []
    haystack_parts: List[str] = []
    ui = (ctx.get("user_input") or {})
    if ui.get("goal_text"):
        haystack_parts.append(str(ui["goal_text"]))
    prop = proposal if isinstance(proposal, dict) else {}
    for rc in (prop.get("recommended_capabilities") or []):
        if isinstance(rc, dict) and rc.get("name"):
            haystack_parts.append(str(rc["name"]))
    haystack = " ".join(haystack_parts).lower()
    etags = {str(t).lower() for t in (expert_tags or []) if t}

    # Optional batch tag lookup by capability id (tags live on CapabilityItem,
    # not in internal_context). Best-effort: any failure → name-only matching.
    tags_by_id: dict = {}
    if session is not None:
        try:
            from app.models.db import CapabilityItem
            from sqlmodel import select
            ids = [c.get("id") for c in caps if isinstance(c, dict) and c.get("id") is not None]
            if ids:
                rows = session.exec(select(CapabilityItem).where(CapabilityItem.id.in_(ids))).all()
                for row in rows:
                    tags_by_id[row.id] = [str(t).lower() for t in (_parse_json_list(row.tags) or [])]
        except Exception:
            tags_by_id = {}

    scored: List[tuple] = []
    for c in caps:
        if not isinstance(c, dict):
            continue
        name = str(c.get("name") or "")
        if not name:
            continue
        cap_tags = tags_by_id.get(c.get("id"), [])
        score = 0
        # Text overlap: name or any tag appearing in the goal/proposal haystack.
        if haystack:
            if name.lower() in haystack:
                score += _TEXT_WEIGHT
            for t in cap_tags:
                if t and t in haystack:
                    score += _TEXT_WEIGHT
        # Expert-template tag overlap (stronger signal).
        if etags and cap_tags:
            for t in cap_tags:
                if t in etags:
                    score += _EXPERT_TAG_WEIGHT
        scored.append((score, {"id": c.get("id"), "type": c.get("type", ""), "name": name}))
    # Prefer hits; fall back to first-N so a candidate list is never empty when
    # capabilities exist (the planner still benefits from seeing options).
    scored.sort(key=lambda t: t[0], reverse=True)
    return [ref for _, ref in scored[:limit]]


# Capability-match scoring weights (T4). Additive + transparent; expert-template
# tag overlap outweighs plain goal-text overlap because the matched template is a
# stronger domain signal than loose keyword presence.
_TEXT_WEIGHT = 1
_EXPERT_TAG_WEIGHT = 3

# Runtime modes the recommender may return (mirrors planning_context.RUNTIME_MODES).
_RUNTIME_MODES = ("direct", "flow", "cron", "multi_agent")
# Keyword → runtime_mode signals, checked in priority order (design D3).
_CRON_KEYWORDS = ("定时", "每天", "每日", "每周", "周期", "日报", "周报", "月报", "cron", "schedule", "定期")
_FLOW_KEYWORDS = ("工作流", "多步", "流程", "审批", "步骤", "workflow", "pipeline", "编排")
_MULTI_AGENT_KEYWORDS = ("多代理", "多智能体", "协同", "multi-agent", "multi_agent", "multiagent", "多 agent")


def recommend_runtime_mode(*, goal_text: str = "", task_classification: str = "",
                           expert_modes: Optional[List[str]] = None) -> str:
    """Heuristically recommend a runtime_mode ∈ {direct, flow, cron, multi_agent}
    (T4 design D3). Priority: cron keywords → flow keywords → multi_agent
    keywords → matched expert template's first valid recommended mode → direct.

    runtime_mode (how it executes) is orthogonal to architecture_pattern (what it
    does); this function never touches architecture_pattern. Return value is
    always within the legal set."""
    hay = f"{goal_text or ''} {task_classification or ''}".lower()
    if any(k.lower() in hay for k in _CRON_KEYWORDS):
        return "cron"
    if any(k.lower() in hay for k in _FLOW_KEYWORDS):
        return "flow"
    if any(k.lower() in hay for k in _MULTI_AGENT_KEYWORDS):
        return "multi_agent"
    for m in (expert_modes or []):
        if str(m) in _RUNTIME_MODES:
            return str(m)
    return "direct"


def _persist_proposal_to_memory(memory: dict, payload, planning_ctx: Optional[dict]) -> None:
    """Mirror the validated proposal into the existing planner memory (T2 task 4.2).

    No new table: the proposal lives in ``planner_sessions`` via the working
    memory (latest_proposal_summary + a structured proposal_payload mirror). The
    decision to keep ``proposals`` as a first-class table is deferred to T5."""
    if payload is None:
        return
    try:
        data = payload.model_dump()
    except Exception:
        return
    if data.get("title") and not memory.get("latest_proposal_summary"):
        memory["latest_proposal_summary"] = data["title"]
    # Structured mirror (plain dict; persisted inside planning_state_json by the
    # existing session serializer). expert_candidates stays empty until T3.
    memory["proposal_payload"] = data
