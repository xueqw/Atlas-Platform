"""System-prompt / memory / skills / replan-context construction + model resolution."""

import json
from typing import Dict, List, Optional
from sqlmodel import Session, select, desc
from app.core.database import engine
from app.models.db import CapabilityItem
from .state import PLANNER_SYSTEM_PROMPT, REPLAN_SYSTEM_PROMPT, DEFAULT_BUILTIN_SKILLS, WORKSPACE_DEFAULT_SKILLS


def _resolve_model(model_id: str) -> dict:
    """Resolve a model selector value to provider + model_name from the single
    source of truth: ``capability_items(type=model)``.

    ``model_id`` may be the model's ``config.model_id`` or its capability
    ``name`` (display name) — both are matched. For capability models the config
    may also carry a custom ``base_url``/``api_key`` (an endpoint not configured
    via provider env vars); those are returned so the run link can use them
    directly. Credentials live only in backend memory and MUST NOT be forwarded
    to the frontend. Endpoint extraction is shared with DAG execution via
    ``app.core.model_caps._capability_endpoint``.
    """
    from app.core.model_caps import _capability_endpoint

    with Session(engine) as session:
        from sqlmodel import select
        from app.models.db import CapabilityItem
        caps = session.exec(
            select(CapabilityItem).where(CapabilityItem.type == "model")
        ).all()
        by_model_id = None
        by_name = None
        for cap in caps:
            if not cap.config:
                continue
            try:
                cfg = json.loads(cap.config) if isinstance(cap.config, str) else cap.config
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(cfg, dict):
                continue
            if cfg.get("model_id") == model_id and by_model_id is None:
                by_model_id = cfg
            if cap.name == model_id and by_name is None:
                by_name = cfg
        cfg = by_model_id or by_name
        if cfg:
            resolved = {
                "model_name": cfg.get("model_id", model_id),
                "provider": cfg.get("provider", "glm"),
            }
            resolved.update(_capability_endpoint(cfg))
            return resolved
    return {"model_name": model_id, "provider": "glm"}


def resolve_effective_skills(
    selected_skills: Optional[List[str]] = None,
    system_builtin_skills: Optional[set] = None,
    workspace_default_skills: Optional[set] = None,
) -> Dict[str, List[str]]:
    """Resolve the three-tier skill model into a single source of truth (D1).

    Returns ``{selected_skills, default_skills, effective_skills}`` where:
      - default_skills   = system_builtin + workspace_default (backend-authoritative)
      - effective_skills = system_builtin + workspace_default + user_selected,
        deduped, fixed order: system_builtin → workspace_default → user_selected.

    Defense (design D3): defaults are ALWAYS in effective_skills regardless of
    what the frontend sent — a default name mistakenly present in selected_skills
    is deduped (counted once, under its default tier) and can never be removed by
    the user layer. None/empty values are filtered. Defaults to the module-level
    constants when sets are not passed."""
    builtin = system_builtin_skills if system_builtin_skills is not None else DEFAULT_BUILTIN_SKILLS
    workspace = workspace_default_skills if workspace_default_skills is not None else WORKSPACE_DEFAULT_SKILLS
    selected = [s for s in (selected_skills or []) if isinstance(s, str) and s]

    ordered: List[str] = []
    seen: set = set()

    def _add(name: str) -> None:
        if name and name not in seen:
            ordered.append(name)
            seen.add(name)

    # Fixed order: builtin → workspace → user. Sort the (unordered) set tiers for
    # stable, reproducible output; user_selected keeps its insertion order.
    for name in sorted(builtin):
        _add(name)
    for name in sorted(workspace):
        _add(name)
    # user_selected: skip names that are defaults (already added) — dedup + the
    # user layer cannot re-introduce or reorder a default.
    default_names = set(builtin) | set(workspace)
    for name in selected:
        if name in default_names:
            continue
        _add(name)

    default_skills = [n for n in ordered if n in default_names]
    return {
        "selected_skills": selected,
        "default_skills": default_skills,
        "effective_skills": ordered,
    }


def _skill_descriptions(names: List[str]) -> Dict[str, str]:
    """Look up live descriptions for skill names from capability_items."""
    names = [n for n in (names or []) if n]
    if not names:
        return {}
    desc_by_name: Dict[str, str] = {}
    with Session(engine) as session:
        rows = session.exec(
            select(CapabilityItem).where(
                CapabilityItem.type == "skill", CapabilityItem.name.in_(names)
            )
        ).all()
        for r in rows:
            desc_by_name[r.name] = r.description or ""
    return desc_by_name


def _skills_block(
    effective_skills: List[str],
    default_skills: Optional[List[str]] = None,
) -> str:
    """Build the skill-injection block from the resolved three-tier model.

    Renders two sections (design D2): 「## 规划师内建能力」 for default skills
    (system_builtin + workspace_default) and 「## 本次会话附加技能」 for the
    user_selected remainder. Each lists name + live description from
    capability_items. Empty sections produce no heading (task 2.3):

    - When there are no user-selected skills, the 「附加技能」 section is omitted.
    - The built-in section only lists defaults that exist as catalog skills with
      a description; ``a2ui`` and friends whose behavior is already hardcoded in
      the system prompt are not repeated verbatim when they carry no catalog
      description, avoiding redundant noise.
    """
    effective = [s for s in (effective_skills or []) if s]
    if not effective:
        return ""
    defaults = set(default_skills or [])
    builtin_names = [n for n in effective if n in defaults]
    user_names = [n for n in effective if n not in defaults]

    desc_by_name = _skill_descriptions(effective)
    out: List[str] = []

    # Built-in section: only surface defaults that add information (have a
    # catalog description). a2ui etc. that are already wired into the prompt and
    # carry no description are skipped to avoid redundant noise (task 2.3).
    builtin_lines = [
        f"- **{n}**：{desc_by_name[n]}"
        for n in builtin_names
        if desc_by_name.get(n)
    ]
    if builtin_lines:
        out.append("\n\n## 规划师内建能力")
        out.append("以下是规划师始终启用的内建能力，作为基础规则使用：")
        out.extend(builtin_lines)

    # Session-attached section: omitted entirely when user_selected is empty.
    if user_names:
        out.append("\n\n## 本次会话附加技能")
        out.append("用户已为本次会话挂载以下技能，请在规划时主动运用其方法论与契约：")
        for n in user_names:
            d = desc_by_name.get(n, "")
            out.append(f"- **{n}**：{d}" if d else f"- **{n}**")

    if not out:
        return ""
    return "\n".join(out) + "\n"


# ── Skill transparency (change: add-planner-skill-transparency) ──────────────
#
# Maps a deterministic backend action to the skill that conventionally owns it,
# for a skill-attributed `activity` and the result badge (design D2). What the
# user sees is which skill ACTUALLY fired this turn — not which skills are merely
# enabled (that is already visible in the skill panel).
#
# Attribution is HEURISTIC: it reflects "this turn performed an action
# conventionally owned by this skill", NOT a token-level proof the model invoked
# it. A skill name is attached ONLY when it is actually in the turn's
# effective_skills (design D5); otherwise the caller surfaces the action without
# claiming a skill, so a non-mounted skill is never falsely labelled as used.

# action_key → (candidate skill names in priority order, friendly action phrase).
# The phrase is a verb phrase describing what the action does (matches the spec
# scenarios "生成结构化确认卡片" / "整理需求摘要与任务分型"); the live catalog
# description is surfaced separately by the frontend as a tooltip/subtitle.
_ACTION_SKILL_MAP: Dict[str, tuple] = {
    "a2ui": (["a2ui"], "生成结构化确认卡片"),
    "proposal": (["deerflow-planner"], "整理需求摘要与任务分型"),
}


def resolve_action_skill(
    action_key: str,
    effective_skills: List[str],
    desc_by_name: Optional[Dict[str, str]] = None,
) -> tuple:
    """Resolve a deterministic backend action to (skill_name | None, action_phrase).

    The skill name is returned ONLY when a candidate is in ``effective_skills``
    this turn (design D5); otherwise None — the action is surfaced without a
    skill label. The phrase is the built-in verb phrase for the action (the
    catalog description, when present, is passed through for the frontend tooltip
    but does not replace the action verb)."""
    candidates, phrase = _ACTION_SKILL_MAP.get(action_key, ([], action_key))
    eff = set(effective_skills or [])
    skill = next((c for c in candidates if c in eff), None)
    return skill, phrase


def _summarize_graph_nodes(graph_json: str) -> str:
    """Summarize a DAG graph_json into a compact per-node description for the
    Replan context (node type + id + key config fields), instead of dumping the
    whole JSON which would blow the prompt budget (design Risk: 注入上下文过大)."""
    try:
        graph = json.loads(graph_json or "{}")
    except (json.JSONDecodeError, TypeError):
        return ""
    nodes = graph.get("nodes") or []
    edges = graph.get("edges") or []
    if not isinstance(nodes, list):
        return ""
    # Per-node-type, the config fields worth surfacing (summarized, not full).
    _KEY_FIELDS = ["role_name", "model_name", "provider", "output_format",
                   "knowledge_binding", "memory_enabled", "memory_strategy", "tools"]
    lines: List[str] = []
    for n in nodes:
        if not isinstance(n, dict):
            continue
        nid = n.get("id", "")
        ntype = n.get("type", "")
        cfg = n.get("config") or {}
        bits: List[str] = []
        for k in _KEY_FIELDS:
            v = cfg.get(k)
            if v in (None, "", [], {}):
                continue
            if k == "system_prompt":
                continue
            if isinstance(v, (list, dict)):
                bits.append(f"{k}={json.dumps(v, ensure_ascii=False)[:80]}")
            else:
                bits.append(f"{k}={v}")
        sp = cfg.get("system_prompt")
        if isinstance(sp, str) and sp.strip():
            bits.append(f"system_prompt≈{sp.strip()[:120]}…")
        suffix = f"（{'; '.join(bits)}）" if bits else ""
        lines.append(f"- [{ntype}] {nid}{suffix}")
    edge_lines: List[str] = []
    for e in edges:
        if not isinstance(e, dict):
            continue
        src = e.get("source", "")
        tgt = e.get("target", "")
        handle = e.get("targetHandle")
        edge_lines.append(f"- {src} → {tgt}" + (f" [{handle}]" if handle else ""))
    out: List[str] = []
    if lines:
        out.append("### 现有节点")
        out += lines
    if edge_lines:
        out.append("### 现有连接")
        out += edge_lines
    return "\n".join(out)


def _build_replan_context(
    agent: "Agent",
    graph_json: str,
    memory: dict,
    trace_summary: Optional[dict] = None,
    eval_summary: Optional[dict] = None,
) -> str:
    """Assemble the Replan context block injected into the system prompt.

    Always includes the existing graph summary + planning metadata. Trace and
    eval evidence are appended only when present (design D4: silent degrade)."""
    parts: List[str] = ["# 现有智能体上下文（重规划基线）", ""]
    parts.append(f"## 目标智能体\n- 名称：{agent.name or '（未命名）'}\n- 描述：{agent.description or '（无）'}")

    graph_summary = _summarize_graph_nodes(graph_json)
    if graph_summary:
        parts.append("## 现有架构\n" + graph_summary)
    else:
        parts.append("## 现有架构\n（无法解析现有 graph，请基于元数据与新需求重规划）")

    meta_lines: List[str] = []
    if memory.get("requirement_summary"):
        meta_lines.append(f"- 需求摘要：{memory['requirement_summary']}")
    if memory.get("task_classification"):
        meta_lines.append(f"- 任务分型：{memory['task_classification']}")
    if memory.get("architecture_pattern"):
        meta_lines.append(f"- 架构模式：{memory['architecture_pattern']}")
    if memory.get("confirmed_constraints"):
        meta_lines.append("- 已确认约束：" + "、".join(str(c) for c in memory["confirmed_constraints"]))
    if meta_lines:
        parts.append("## 规划元数据\n" + "\n".join(meta_lines))

    # Prior proposal summary — so replan revises a known baseline rather than
    # re-deriving it (task 5.2).
    if memory.get("latest_proposal_summary"):
        parts.append("## 上一版方案概要\n" + str(memory["latest_proposal_summary"]))

    # Current memory/knowledge/evaluation strategy summary — replan should
    # explicitly carry these forward or adjust them (task 5.2 / spec: 同步策略).
    strat_lines: List[str] = []
    for key, label in (
        ("memory_strategy", "记忆策略"),
        ("knowledge_strategy", "知识策略"),
        ("evaluation_strategy", "评估策略"),
    ):
        summ = _strategy_summary(memory.get(key) or {})
        if summ:
            strat_lines.append(f"- {label}：{summ}")
    if strat_lines:
        parts.append("## 现有策略（如需调整请在 proposal 中显式说明）\n" + "\n".join(strat_lines))

    # Any prior issue_analysis carried in memory — gives the replan continuity
    # on what was already attributed (task 5.2).
    prior_issues = memory.get("issue_analysis")
    if isinstance(prior_issues, list) and prior_issues:
        il: List[str] = []
        for it in prior_issues[:5]:
            if not isinstance(it, dict):
                continue
            cat = str(it.get("category") or "")
            act = str(it.get("proposed_action") or "")
            il.append(f"- [{cat}] {act}".rstrip())
        if il:
            parts.append("## 既有问题归因\n" + "\n".join(il))

    # Optional evidence — silently omitted when unavailable.
    if trace_summary:
        tl = [
            f"- 最近运行状态：{trace_summary.get('status', '未知')}",
            f"- 平均耗时：{trace_summary.get('avg_duration_ms', 0)} ms",
            f"- 错误次数：{trace_summary.get('error_count', 0)} / {trace_summary.get('run_count', 0)} 次",
        ]
        err = trace_summary.get("last_error")
        if err:
            tl.append(f"- 最近错误：{str(err)[:200]}")
        parts.append("## 最近运行证据\n" + "\n".join(tl))
    if eval_summary:
        el = [
            f"- 最近评估通过率：{eval_summary.get('pass_rate_pct', '—')}",
            f"- 关键 case：{eval_summary.get('key_passed', 0)}/{eval_summary.get('key_total', 0)} 通过",
        ]
        parts.append("## 最近评估证据\n" + "\n".join(el))

    return "\n\n".join(parts)


def _load_trace_summary(session: Session, agent_id: int, limit: int = 10) -> Optional[dict]:
    """Aggregate the most recent AgentRunSummary rows for an agent (main line A).

    Returns None when no runs exist so the Replan context omits the block."""
    from app.models.db import AgentRunSummary
    rows = session.exec(
        select(AgentRunSummary)
        .where(AgentRunSummary.agent_id == agent_id)
        .order_by(desc(AgentRunSummary.created_at))
        .limit(limit)
    ).all()
    if not rows:
        return None
    run_count = len(rows)
    error_count = sum(1 for r in rows if r.status == "error")
    avg_duration = round(sum(r.total_duration_ms for r in rows) / run_count) if run_count else 0
    last_error = next((r.error for r in rows if r.error), "")
    return {
        "run_count": run_count,
        "error_count": error_count,
        "avg_duration_ms": avg_duration,
        "status": rows[0].status,
        "last_error": last_error,
    }


def _load_eval_summary(session: Session, agent_id: int) -> Optional[dict]:
    """Summarize the latest EvaluationRun for an agent (main line B).

    Returns None when no evaluation has run so the Replan context omits it."""
    from app.models.db import EvaluationRun
    run = session.exec(
        select(EvaluationRun)
        .where(EvaluationRun.agent_id == agent_id)
        .order_by(desc(EvaluationRun.created_at))
    ).first()
    if run is None:
        return None
    try:
        summary = json.loads(run.summary or "{}")
        summary = summary if isinstance(summary, dict) else {}
    except (json.JSONDecodeError, TypeError):
        summary = {}
    pass_rate = summary.get("pass_rate")
    return {
        "pass_rate_pct": f"{round(pass_rate * 100)}%" if isinstance(pass_rate, (int, float)) else "—",
        "key_passed": summary.get("key_passed", 0),
        "key_total": summary.get("key_total", 0),
        "passed": run.passed,
    }


def _summarize_decisions(decisions: List[dict], limit: int = 8) -> List[str]:
    """One line per confirmed decision for prompt injection (task 3.3).

    Keeps the most recent ``limit`` so the ledger can grow without bloating the
    prompt. Each line surfaces topic + chosen option (+ note), so the planner
    treats the decision as settled and does not re-ask."""
    lines: List[str] = []
    for d in (decisions or [])[-limit:]:
        if not isinstance(d, dict):
            continue
        topic = str(d.get("topic") or d.get("prompt") or "").strip()
        sel = d.get("selected_option") or {}
        label = str(sel.get("label") or sel.get("id") or "").strip() if isinstance(sel, dict) else ""
        free = str(d.get("free_text") or "").strip()
        if not topic and not label:
            continue
        line = f"- {topic or '(决策)'}：已选「{label or '—'}」"
        if free:
            line += f"（备注：{free}）"
        lines.append(line)
    return lines


def _strategy_summary(strategy: dict) -> str:
    """Compact one-line summary of a strategy dict (design D3: 取摘要不 dump 全量)."""
    if not isinstance(strategy, dict) or not strategy:
        return ""
    bits: List[str] = []
    for k, v in list(strategy.items())[:4]:
        if isinstance(v, (list, dict)):
            v = json.dumps(v, ensure_ascii=False)
        sval = str(v)
        if len(sval) > 60:
            sval = sval[:57] + "…"
        bits.append(f"{k}={sval}")
    return "；".join(bits)


def _build_system_prompt_with_memory(
    memory: dict,
    mode: str = "create",
    replan_context: str = "",
) -> str:
    """Inject memory context into the system prompt.

    Replan-mode sessions use REPLAN_SYSTEM_PROMPT and prepend the assembled
    existing-agent context block; create-mode keeps the original behavior.

    richer state (open_questions / assumptions / risks / decisions_confirmed /
    apply_readiness …) is injected summary-style — lists are capped, strategy
    dicts are summarized rather than dumped (design D3)."""
    base = REPLAN_SYSTEM_PROMPT if mode == "replan" else PLANNER_SYSTEM_PROMPT
    if mode == "replan" and replan_context:
        base = base + "\n\n" + replan_context
    # Skills are injected based on the resolved effective set (three-tier model),
    # not the raw selected_skills — defaults are always present even if the
    # frontend didn't send them (design D2/D3). Rendered as two sections.
    resolved = resolve_effective_skills((memory or {}).get("selected_skills") or [])
    base += _skills_block(resolved["effective_skills"], resolved["default_skills"])

    memory = memory or {}
    # Whether any context worth injecting exists (legacy fields OR richer state).
    _richer_keys = [
        "open_questions", "assumptions", "non_goals", "success_metrics",
        "decisions_pending", "decisions_confirmed", "tradeoffs", "risks",
        "architecture_pattern", "runtime_strategy", "memory_strategy",
        "knowledge_strategy", "evaluation_strategy",
    ]
    has_legacy = any(
        memory.get(k) for k in ["requirement_summary", "confirmed_constraints", "task_classification"]
    )
    has_richer = any(memory.get(k) for k in _richer_keys)
    readiness = memory.get("apply_readiness") or {}
    has_readiness = isinstance(readiness, dict) and str(readiness.get("status") or "not_ready") != "not_ready"
    if not (has_legacy or has_richer or has_readiness):
        return base

    ctx = "\n\n# 当前规划记忆（上下文连续性）\n以下是之前对话中已确认的信息，请基于此继续规划，不要遗忘或重复询问已确认内容：\n"
    if memory.get("requirement_summary"):
        ctx += f"\n## 需求摘要\n{memory['requirement_summary']}\n"
    if memory.get("task_classification"):
        ctx += f"\n## 任务分型\n{memory['task_classification']}\n"
    if memory.get("architecture_pattern"):
        ctx += f"\n## 架构模式\n{memory['architecture_pattern']}\n"
    if memory.get("confirmed_constraints"):
        ctx += "\n## 已确认约束\n" + "\n".join(f"- {c}" for c in memory["confirmed_constraints"][:8]) + "\n"
    # Decision ledger — the planner MUST NOT re-ask confirmed items (spec).
    decision_lines = _summarize_decisions(memory.get("decisions_confirmed") or [])
    if decision_lines:
        ctx += "\n## 已确认决策（勿重复询问）\n" + "\n".join(decision_lines) + "\n"
    if memory.get("non_goals"):
        ctx += "\n## 明确不做（non-goals）\n" + "\n".join(f"- {g}" for g in memory["non_goals"][:5]) + "\n"
    if memory.get("open_questions"):
        ctx += "\n## 待澄清问题\n" + "\n".join(f"- {q}" for q in memory["open_questions"][:5]) + "\n"
    if memory.get("assumptions"):
        ctx += "\n## 当前假设\n" + "\n".join(f"- {a}" for a in memory["assumptions"][:5]) + "\n"
    if memory.get("success_metrics"):
        ctx += "\n## 成功标准\n" + "\n".join(f"- {m}" for m in memory["success_metrics"][:5]) + "\n"
    if memory.get("tradeoffs"):
        ctx += "\n## 关键取舍\n" + "\n".join(f"- {t}" for t in memory["tradeoffs"][:5]) + "\n"
    if memory.get("risks"):
        ctx += "\n## 风险\n" + "\n".join(f"- {r}" for r in memory["risks"][:5]) + "\n"
    # Strategy fields — summarized, not full JSON dumped (design D3).
    for key, label in (
        ("runtime_strategy", "运行策略"),
        ("memory_strategy", "记忆策略"),
        ("knowledge_strategy", "知识策略"),
        ("evaluation_strategy", "评估策略"),
    ):
        summ = _strategy_summary(memory.get(key) or {})
        if summ:
            ctx += f"\n## {label}\n{summ}\n"
    if memory.get("latest_proposal_summary"):
        ctx += f"\n## 最新方案概要\n{memory['latest_proposal_summary']}\n"
    if has_readiness:
        miss = readiness.get("missing") or []
        rec = str(readiness.get("recommendation") or "")
        line = f"状态：{readiness.get('status')}"
        if miss:
            line += "；待补：" + "、".join(str(m) for m in miss[:5])
        if rec:
            line += f"；建议：{rec}"
        ctx += f"\n## 落地就绪度\n{line}\n"
    if memory.get("user_feedback"):
        ctx += "\n## 用户反馈\n" + "\n".join(f"- {f}" for f in memory["user_feedback"][:8]) + "\n"
    return base + ctx


def _parse_skill_config(raw: str) -> dict:
    try:
        cfg = json.loads(raw or "{}")
        return cfg if isinstance(cfg, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


# ── Planning Context injection (T2: planner-proposal-flow, design D1) ─────────

def _render_context_block(ctx: dict) -> str:
    """Render the seven-segment planning_context into a compact, readable prompt
    block (NOT raw JSON — readable structure is more stable for the model and
    lets the planner cite it in context_used_explanation).

    Summary-style by construction: capabilities show only id/type/name, memory
    is already top-N summaries from the aggregator, and nothing carries raw
    markdown / prompt content. An empty/default context yields a minimal block
    (still injected so the planner knows the segments exist)."""
    if not isinstance(ctx, dict) or not ctx:
        return ""
    parts: List[str] = [
        "\n\n# 规划上下文（Planning Context）",
        "以下是系统聚合的多源规划语境，请优先据此内部推理、先出方案、仅关键缺口才澄清：",
    ]

    prof = ctx.get("user_profile") or {}
    prof_bits: List[str] = []
    for key, label in (("role", "角色"), ("department", "部门"), ("industry", "行业")):
        if prof.get(key):
            prof_bits.append(f"{label}={prof[key]}")
    prefs = prof.get("preferences") or {}
    if isinstance(prefs, dict) and prefs:
        prof_bits.append("偏好=" + _compact(prefs))
    if prof_bits:
        parts.append("## 用户画像\n" + "；".join(prof_bits))

    ws = ctx.get("workspace_context") or {}
    ws_bits: List[str] = []
    if ws.get("project_context"):
        ws_bits.append(f"业务背景={ws['project_context']}")
    if ws.get("connected_systems"):
        ws_bits.append("已接入系统=" + "、".join(str(s) for s in ws["connected_systems"]))
    if ws.get("default_entities"):
        ws_bits.append("默认实体=" + "、".join(str(e) for e in ws["default_entities"]))
    if ws.get("default_capability_tags"):
        ws_bits.append("能力标签=" + "、".join(str(t) for t in ws["default_capability_tags"]))
    if ws_bits:
        parts.append("## 工作区\n" + "；".join(ws_bits))

    ext = (ctx.get("external_context") or {}).get("systems") or []
    if ext:
        ext_lines = []
        for sysd in ext[:8]:
            if not isinstance(sysd, dict):
                continue
            ents = "、".join(str(e) for e in (sysd.get("entities") or []))
            ops = "、".join(str(o) for o in (sysd.get("operations") or []))
            ext_lines.append(f"- {sysd.get('system', '')}：实体[{ents}] 操作[{ops}]")
        if ext_lines:
            parts.append("## 外部系统（实体与操作）\n" + "\n".join(ext_lines))

    mem = ctx.get("memory_context") or {}
    prefs_mem = mem.get("preference_memory") or []
    acts = mem.get("recent_activity_summary") or []
    mem_lines: List[str] = []
    for p in prefs_mem[:8]:
        if isinstance(p, dict) and (p.get("value") or p.get("key")):
            mem_lines.append(f"- 偏好：{p.get('value') or p.get('key')}")
    for a in acts[:5]:
        if a:
            mem_lines.append(f"- 近期：{a}")
    if mem_lines:
        parts.append("## 历史偏好与近期活动\n" + "\n".join(mem_lines))

    internal = ctx.get("internal_context") or {}
    caps = internal.get("capabilities") or []
    if caps:
        cap_lines = [
            f"- [{c.get('type', '')}] {c.get('name', '')} (id={c.get('id')})"
            for c in caps[:40] if isinstance(c, dict)
        ]
        parts.append(
            "## 平台可用能力（结构化引用，推荐时引用这些，勿臆造）\n" + "\n".join(cap_lines)
        )
    if internal.get("runtime_modes"):
        parts.append("## 可选运行模式\n" + "、".join(str(m) for m in internal["runtime_modes"]))

    pol = ctx.get("policy_context") or {}
    pol_bits: List[str] = []
    if pol.get("clarification_policy"):
        pol_bits.append(f"澄清策略={pol['clarification_policy']}")
    if pol.get("publish_policy"):
        pol_bits.append(f"发布策略={pol['publish_policy']}")
    if pol.get("runtime_restrictions"):
        pol_bits.append("运行限制=" + "、".join(str(r) for r in pol["runtime_restrictions"]))
    if pol_bits:
        parts.append("## 治理边界\n" + "；".join(pol_bits))

    return "\n\n".join(parts) + "\n"


def _compact(obj, limit: int = 80) -> str:
    """One-line JSON of a small dict/list, truncated — for prompt economy."""
    try:
        s = json.dumps(obj, ensure_ascii=False)
    except (TypeError, ValueError):
        s = str(obj)
    return s if len(s) <= limit else s[: limit - 1] + "…"
