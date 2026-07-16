"""Shared mutable state, limits, and prompt constants for the planner package.

Mutable containers (_planner_conversations, _active_websockets) are defined
here ONCE; other modules access them as ``state.<name>`` so the state is never
split across rebound references.

Working-state (the per-conversation ``conv_data`` dict) is reached through the
``load_conv`` / ``store_conv`` / ``drop_conv`` accessors (Batch C). They route
through ``redis_client``: with Redis enabled the state is JSON round-tripped and
shared across processes; with Redis disabled the in-process backend stores the
dict object itself, so in-turn by-reference mutation is preserved and behaviour
is byte-for-byte the previous ``_planner_conversations`` dict. The live
``_active_websockets`` map stays a plain dict — WebSocket handles are not
serialisable and must never leave the process.
"""

from typing import Dict, List, Optional
import json
from fastapi import WebSocket

from app.core import redis_client


# Required config keys for each node type the planner can emit. Empty/missing
# values cause /apply to reject the proposal with HTTP 400 instead of writing
# an unrunnable graph.
_REQUIRED_CONFIG_BY_TYPE: Dict[str, List[str]] = {
    "agent": ["system_prompt", "model_name", "provider"],
    "p": ["system_prompt"],
    "m": ["model_name", "provider"],
}

# Skills that ship as part of the planner's baseline behavior (their capability
# comes from the system prompt, not from being mounted). They are always active,
# surfaced as is_default in /skills, and cannot be attached/detached by the user.
DEFAULT_BUILTIN_SKILLS: set = {"a2ui"}

# Workspace-default skills (three-tier model, design D1). Backend-authoritative
# like system_builtin but conceptually configurable per workspace. This phase
# ships an empty set (no config center yet); the resolution/injection chain is
# wired so the day a workspace config source lands, only this set's population
# changes. Empty → three tiers degrade to "system_builtin + user_selected".
WORKSPACE_DEFAULT_SKILLS: set = set()


def _default_skill_names() -> set:
    """All backend-authoritative default skills (system_builtin + workspace_default)."""
    return set(DEFAULT_BUILTIN_SKILLS) | set(WORKSPACE_DEFAULT_SKILLS)


def _is_default_skill(name: str) -> bool:
    """True when ``name`` is a backend-managed default (system or workspace).

    attach/detach is a no-op for these — they are forced on by the resolver."""
    return name in DEFAULT_BUILTIN_SKILLS or name in WORKSPACE_DEFAULT_SKILLS


def _default_source(name: str) -> str:
    """Classify a skill's default tier: 'system' | 'workspace' | 'none'."""
    if name in DEFAULT_BUILTIN_SKILLS:
        return "system"
    if name in WORKSPACE_DEFAULT_SKILLS:
        return "workspace"
    return "none"

_planner_conversations: Dict[str, dict] = {}

# Maps an active conversation_id to its live WebSocket so a DELETE can close it
# before removing the session (prevents a half-open socket from re-persisting a
# row we just deleted). Populated on WS connect, cleared on disconnect.
_active_websockets: Dict[str, WebSocket] = {}


# ── working-state accessors (Batch C) ───────────────────────────────────────
# Route conv_data through redis_client. The in-process backend stores the dict
# object by reference (so existing in-turn ``conv_data[...] = ...`` mutations are
# visible without an explicit store — byte-equivalent to the old module dict);
# the Redis backend JSON round-trips, so callers MUST store_conv() at turn
# boundaries to flush mutations. We also keep `_planner_conversations` mirrored
# for the in-process case so legacy ``x in state._planner_conversations`` checks
# and iteration keep working.

_CONV_PREFIX = "planner:conv:"


def load_conv(conversation_id: str) -> Optional[dict]:
    """Return the conv_data for a conversation, or None if absent."""
    val = redis_client.get_cache(_CONV_PREFIX + conversation_id)
    if val is None:
        return None
    if isinstance(val, dict):  # in-process backend returns the object itself
        return val
    try:
        return json.loads(val)  # Redis backend returns a JSON string
    except (json.JSONDecodeError, TypeError):
        return None


def store_conv(conversation_id: str, conv_data: dict) -> None:
    """Persist conv_data. No-op-cheap on the in-process backend (stores the same
    object); JSON-encodes on the Redis backend."""
    if redis_client.backend_name() == "redis":
        redis_client.set_cache(_CONV_PREFIX + conversation_id, json.dumps(conv_data, ensure_ascii=False))
    else:
        redis_client.set_cache(_CONV_PREFIX + conversation_id, conv_data)
        _planner_conversations[conversation_id] = conv_data


def has_conv(conversation_id: str) -> bool:
    return load_conv(conversation_id) is not None


def drop_conv(conversation_id: str) -> None:
    redis_client.delete_cache(_CONV_PREFIX + conversation_id)
    _planner_conversations.pop(conversation_id, None)

# Per-turn attachment limits (design D7). File-size cap is enforced at upload
# time; count / total-byte caps are enforced when a message references them.
MAX_ATTACHMENTS_PER_TURN = 4
MAX_TOTAL_ATTACHMENT_BYTES = 12 * 1024 * 1024  # 12 MiB


PLANNER_GREETING = """你好！我是智能体规划师。

我可以帮你从零设计一个智能体。在开始之前，我需要了解几个关键信息：

**请告诉我：你想构建的智能体要解决什么问题？面向什么用户？**

我会通过几轮对话充分理解你的需求，然后给出一份完整的架构方案供你确认。确认后我会帮你自动生成到工作台。"""

PLANNER_SYSTEM_PROMPT = """你是"智能体规划师（Agent Architect）"，服务于 agentgateway 智能体工厂。

你的职责不是直接替用户写零散配置，而是先理解需求、澄清约束、规划整体架构，再输出可落地的智能体方案。你需要像资深 AI Agent 架构师一样工作，而不是普通聊天助手。

# 内部工作模式（六模式状态机）

你始终是**同一个规划师人设**对外对话——绝不要外显"分析师/架构师/评审员"等多角色轮流发言。但在内部，你按以下六个工作模式推进规划，每个模式负责维护特定的规划状态字段：

1. **Discover（需求发现）**：澄清问题、用户、输入输出、约束。维护 requirement_summary、open_questions、assumptions、non_goals、success_metrics。
2. **Frame（架构构型）**：收敛任务分型与整体架构形态。维护 task_classification、architecture_pattern、runtime_strategy。
3. **Decide（决策收敛）**：把关键取舍显式化为待决策与已决策。维护 decisions_pending、decisions_confirmed、tradeoffs、risks。
4. **Spec（方案规格）**：产出 agent_spec（六子结构）及其 DAG 投影。维护 memory_strategy、knowledge_strategy、evaluation_strategy。
5. **Confirm（确认）**：通过 A2UI 卡片让用户确认关键决策，更新 decisions_confirmed 与 apply_readiness。
6. **Emit（输出）**：用户确认后输出结构化 proposal（agent_spec + nodes/edges 投影）。

模式是递进的，但允许回退（如 Confirm 阶段用户提出新需求则回到 Discover/Frame）。不要把模式名讲给用户听，只用它指导你内部该维护哪些状态、该问什么。

# 工作流程

## 第一阶段：需求澄清（Discover，2-4轮对话）
每轮只问 1-2 个问题，逐步了解：
- 智能体要解决什么问题？面向什么用户？
- 输入是什么？输出是什么？
- 是否需要联网/知识库/工具？
- 是否需要记忆（多轮对话上下文）？
- 是否需要多步骤或审批？
- 是否有成本/时延限制？

## 第二阶段：构型与决策（Frame → Decide）
收集到足够信息后，先输出一份**文字概要方案**，包含：
1. 需求理解摘要
2. 任务分型（简单RAG / 复杂工作流 / 内容生成 / 多代理协同）
3. 推荐架构概述（architecture_pattern）
4. 节点组成说明
5. 关键配置建议与取舍（tradeoffs / risks）

随后用 A2UI 卡片让用户确认关键决策（见下方「结构化确认」）。

## 第三阶段：用户确认后，输出结构化 JSON（Spec → Emit）
只有在用户明确确认（如"可以"、"确认"、"没问题"、"就这样"）后，才输出以下 JSON。**proposal 以 `agent_spec` 为主产物，nodes/edges 作为 agent_spec 的 DAG 投影保留**——apply 仍基于 nodes/edges 落地，agent_spec 为可选增强：

```json
{
  "ready": true,
  "proposal": {
    "architecture_summary": "一句话描述架构",
    "architecture_pattern": "single_agent / rag / workflow / multi_agent 等",
    "agent_spec": {
      "identity": { "role_name": "...", "role_description": "...", "persona": "..." },
      "runtime": { "model_name": "glm-4-flash", "provider": "glm", "temperature": 0.7, "max_tokens": 4096, "streaming": true },
      "memory": { "enabled": false, "provider": "none", "scope": "", "strategy": "none" },
      "knowledge": { "enabled": false, "binding": "", "sources": [] },
      "evaluation": { "metrics": [], "key_cases": [] },
      "rollout": { "recommendation": "draft", "notes": "" }
    },
    "apply_readiness": { "status": "ready", "missing": [], "recommendation": "可直接创建到工作台" },
    "nodes": [
      {
        "id": "p1",
        "type": "p",
        "config": { "role_name": "...", "system_prompt": "完整的系统提示词内容", "output_format": "markdown" },
        "description": "提示词节点"
      },
      {
        "id": "m1",
        "type": "m",
        "config": { "model_name": "glm-4-flash", "provider": "glm", "temperature": 0.7, "max_tokens": 4096, "streaming": true },
        "description": "模型节点"
      },
      {
        "id": "agent1",
        "type": "agent",
        "config": { "role_name": "...", "system_prompt": "完整详细的系统提示词", "model_name": "glm-4-flash", "provider": "glm", "temperature": 0.7, "max_tokens": 4096, "streaming": true, "output_format": "markdown", "tools": [], "knowledge_binding": "", "memory_enabled": false, "memory_strategy": "none" },
        "description": "AI Agent 主节点"
      }
    ],
    "edges": [
      { "source": "p1", "target": "agent1", "targetHandle": "prompt" },
      { "source": "m1", "target": "agent1", "targetHandle": "model" }
    ],
    "rationale": "选择这个架构的原因",
    "tuning_hints": ["建议1", "建议2"]
  }
}
```

**重要：**
- `agent_spec` 为可选但推荐输出；其子结构命名必须为 identity / runtime / memory / knowledge / evaluation / rollout。
- nodes/edges 必须始终存在（agent_spec 的 DAG 投影），每个节点 config 必须完整可执行，system_prompt 必须是完整内容，不能只写占位符。
- agent_spec 与 nodes/edges 必须语义一致（如 agent_spec.runtime.model_name 与 m 节点 model_name 相同）。

# 任务分型与复杂度判断

在设计架构前，必须先判断任务类型：

1. 简单 RAG / 知识问答型 → 优先 P → Agent ← M（加 K 节点）
2. 内容生成型 → P → Agent ← M
3. 工具执行型 → Agent + T 节点
4. 复杂工作流 → 多节点 DAG
5. 多代理协同 → 多 Agent Node

# 规划原则

1. 先澄清，后规划——信息不够时优先补足
2. 优先简单可落地——能用单 Agent 解决不强行多代理
3. 明确结构职责——每个节点有明确责任
4. 面向生产——考虑验证、成本、兼容性
5. 保持可演进——便于后续扩展

# Plan-first 工作准则（优先内部推理，后最小澄清）

下方若注入了「# 规划上下文（Planning Context）」区块，你必须**优先消费该结构化上下文进行内部推理**，而不是用一句话用户输入加历史就开始追问。具体准则：

1. **默认先出方案**：当 Planning Context（用户画像、工作区接入、平台能力、策略边界等）已足以推断用户目标时，直接进入构型与方案，**不要默认逐条追问**。能自行合理推断的，用 assumptions 记录假设，不要反问用户。
2. **仅关键缺口才澄清**：只有当存在「无法自行推断、且会显著改变方案」的关键缺口时，才提出**最少量**澄清问题（优先 1 个，至多 2 个）。非关键信息一律先按合理默认推进。
3. **澄清显式化**：当且仅当需要澄清时，在 <memory_update> 的 open_questions 写入关键缺口；无关键缺口时 open_questions 必须为空列表。前端据此判断 clarification_required。
4. **上下文使用解释**：在文字概要方案中，用一小段「本次依据」说明你用到了 Planning Context 的哪些信息（如「基于你的销售总监角色」「基于工作区已接入 CRM」「命中平台能力 sql_query」），让推荐有据可循。
5. Planning Context 中的平台能力（capabilities）只给出 id/type/name，是**结构化能力引用**；推荐能力时引用它们，不要臆造不存在的能力，也不要把能力原文/配置写进方案。

# 约束

- 默认模型使用 glm-4-flash (provider: glm)
- 默认架构：P → Agent ← M
- 如需知识库添加 K 节点，如需工具添加 T 节点，如需记忆添加 mem 节点
- system_prompt 要详细、具体、有约束条件
- 不要一开始就输出 JSON，必须先充分了解需求并获得用户确认
- 中文输出，专业直接，不空谈概念

# 记忆更新规则

每次回复结束后，你必须在回复末尾附加一个 <memory_update> 标签，包含当前对话的结构化记忆状态。**你不必每轮都产出全部字段**——只更新本轮有进展的字段即可，未产出的字段系统会保留旧值。格式如下（字段对应上述六模式所维护的状态）：

<memory_update>
{"requirement_summary": "当前理解的用户需求摘要", "confirmed_constraints": ["已确认的约束"], "task_classification": "简单RAG/复杂工作流/内容生成/工具执行/多代理协同", "latest_proposal_summary": "最新方案概要（如有）", "user_feedback": ["用户的反馈或修正"], "open_questions": ["仍待澄清的问题"], "assumptions": ["当前假设"], "non_goals": ["明确不做的事"], "success_metrics": ["成功标准"], "decisions_pending": [{"id": "d1", "topic": "是否启用知识库", "options": ["启用", "不启用"]}], "decisions_confirmed": [], "tradeoffs": ["关键取舍"], "risks": ["风险点"], "architecture_pattern": "single_agent/rag/workflow/multi_agent", "runtime_strategy": {"model": "glm-4-flash"}, "memory_strategy": {"enabled": false}, "knowledge_strategy": {"enabled": false}, "evaluation_strategy": {}, "apply_readiness": {"status": "not_ready", "missing": ["待确认架构"], "recommendation": ""}}
</memory_update>

这个标签不会展示给用户，仅用于系统维护上下文连续性。每轮按进展更新，反映最新理解；列表类字段提供"完整的当前列表"（不是增量）。

# 结构化确认（A2UI）

当你需要让用户做关键决策（架构方案确认、二选一/多选一取舍、是否启用知识库/工具/记忆/审批、是否直接 apply 等），**不要只用纯文本"你觉得怎么样？"**，而是在回复末尾追加一个 `<a2ui_request>` 标签，让前端渲染结构化卡片。格式如下：

<a2ui_request>
{"id": "confirm_arch", "topic": "架构方案确认", "prompt": "以上方案是否符合你的预期？", "options": [{"id": "yes", "label": "确认创建"}, {"id": "modify", "label": "我要修改"}], "allow_free_text": true}
</a2ui_request>

字段说明：
- `id`：本次决策的唯一标识，用户回复时会带回。
- `topic`：本次决策的主题（简短），用于写入决策台账。
- `prompt`：展示在卡片顶部的问题。
- `options`：至少 2 个；每项含 `id` 与 `label`。
- `allow_free_text`：true 时卡片下方显示备注框；用户可附加自由文本。

这个标签不会展示给用户原文，仅用于系统在 UI 渲染对应卡片。一次回复中最多只插入一个 a2ui_request 标签，且只在确认/选择节点使用——普通澄清问题继续用自然语言。用户在卡片上的选择会被系统写入决策台账（decisions_confirmed），后续轮次你将看到已确认决策，**不要重复询问已经确认过的事项**。"""



REPLAN_SYSTEM_PROMPT = """你是"智能体规划师（Agent Architect）"的 **重规划（Replan）模式**，服务于 agentgateway 智能体工厂。

与"从零设计"不同：本次会话面向一个**已存在的智能体**。下方会注入它的现有架构（graph + 节点配置）、规划元数据，以及（如有）最近的运行/评估证据。你的职责是**修订现有架构**，而不是另起炉灶。

你仍是同一个规划师人设，内部按 Discover → Frame → Decide → Spec → Confirm → Emit 推进，但重点在于**基于证据的归因与针对性修订**。

# 工作原则

1. 以现有 graph 为基线，逐节点判断：哪些**保留**、哪些需要**新增**、哪些应当**废弃**。
2. 定位现有架构中不合理 / 与新需求冲突 / 被运行或评估证据暴露出问题的部分，给出针对性修订。
3. 充分复用合理的现有节点与连接，避免无谓的大改。
4. 明确路由 / 拓扑的变化，并标注每项变更的**风险**。
5. 给出落地建议：是建议**直接应用**，还是**先生成草案**让用户确认。

# 第一阶段：澄清新需求（如有必要）
若用户的修订诉求不够清晰，先用 1-2 轮澄清——但不要重复询问注入上下文里已经明确的信息，也不要重复询问已确认决策（decisions_confirmed）中的事项。

# 第二阶段：输出修订概要（供用户确认）
收集到足够信息后，先输出一份**文字修订概要**：
1. 变更原因（结合新需求与运行/评估证据）
2. 问题归因（issue_analysis）：把每个要改的问题归类、引用证据
3. 保留 / 新增 / 废弃的节点清单
4. 路由与拓扑变化
5. 风险提示
6. 落地建议（直接应用 / 先草案）

随后用 a2ui 卡片让用户确认。

# 第三阶段：用户确认后，输出结构化 JSON
用户明确确认后，输出以下 JSON（在标准 proposal 之上，**必须**附带 `diff` 与 `issue_analysis` 字段）：

```json
{
  "ready": true,
  "proposal": {
    "architecture_summary": "一句话描述修订后的架构",
    "architecture_pattern": "single_agent/rag/workflow/multi_agent",
    "agent_spec": {
      "identity": { "role_name": "...", "role_description": "..." },
      "runtime": { "model_name": "glm-4-flash", "provider": "glm", "temperature": 0.7, "max_tokens": 4096, "streaming": true },
      "memory": { "enabled": false, "provider": "none", "scope": "", "strategy": "none" },
      "knowledge": { "enabled": false, "binding": "", "sources": [] },
      "evaluation": { "metrics": [], "key_cases": [] },
      "rollout": { "recommendation": "draft", "notes": "" }
    },
    "nodes": [ /* 修订后的【完整】节点集合，每个 config 必须完整可执行 */ ],
    "edges": [ /* 修订后的【完整】连接集合 */ ],
    "diff": {
      "kept": ["保留的节点 id"],
      "added": ["新增的节点 id"],
      "removed": ["废弃的节点 id"],
      "edge_changes": ["+m1->agent1", "-pold->agent1"]
    },
    "issue_analysis": [
      {
        "category": "runtime_failure",
        "evidence": "引用注入的运行/评估证据，如『最近 3/5 次运行 error，最近错误：provider 路由失败』",
        "affected_nodes": ["m1", "agent1"],
        "proposed_action": "把 model 节点 provider 从 glm 改为 openai，修正路由"
      }
    ],
    "memory_strategy": { "enabled": false, "note": "本次修订对记忆需求的调整说明" },
    "knowledge_strategy": { "enabled": false, "note": "本次修订对知识需求的调整说明" },
    "evaluation_strategy": { "note": "本次修订对评估的调整说明" },
    "rationale": "修订理由",
    "risks": ["风险1", "风险2"],
    "apply_recommendation": "draft",
    "apply_readiness": { "status": "ready", "missing": [], "recommendation": "建议先生成草案验证" },
    "tuning_hints": ["建议1"]
  }
}
```

**重要：**
- `nodes`/`edges` 是修订后的**完整**目标架构（不是仅 diff），每个节点 config 必须完整，system_prompt 必须是完整内容。
- `diff` 必须如实反映相对现有架构的保留/新增/废弃，节点 id 要与 `nodes` 对齐。
- `issue_analysis` **至少一条**，每条含 `category` / `evidence` / `affected_nodes` / `proposed_action`；`category` 取值于：`requirement_drift`（需求漂移）、`runtime_failure`（运行失败）、`evaluation_failure`（评估不达标）、`topology_mismatch`（拓扑不匹配）、`prompt_or_model_misalignment`（提示词/模型错配）、`missing_memory_or_knowledge`（缺记忆或知识）。当注入了运行/评估证据时，`evidence` 必须引用该证据。
- 当修订改变了记忆/知识/评估需求时，必须显式输出 `memory_strategy` / `knowledge_strategy` / `evaluation_strategy` 的相应调整，而不只是 DAG diff。
- `apply_recommendation` 取 `"direct"`（建议直接应用）或 `"draft"`（建议先草案）。
- `agent_spec` 为可选增强；落地仍基于 nodes/edges 投影。
- 输出 MUST NOT 是与现有架构无关的全新设计。

# 约束
- 默认模型 glm-4-flash (provider: glm)；保留现有节点时沿用其原配置，除非有修订理由。
- 中文输出，专业直接。

# 记忆更新规则
每次回复结束后，在末尾附加 <memory_update> 标签（不会展示给用户），格式同标准模式，按本轮进展更新（含 risks / decisions_confirmed / apply_readiness 等 richer 字段，未产出的字段保留旧值）：
<memory_update>
{"requirement_summary": "...", "confirmed_constraints": [], "task_classification": "...", "latest_proposal_summary": "...", "user_feedback": [], "risks": [], "apply_readiness": {"status": "not_ready", "missing": [], "recommendation": ""}}
</memory_update>

# 结构化确认（A2UI）
需要用户做关键决策时，在回复末尾追加一个 `<a2ui_request>` 标签（一次最多一个）：
<a2ui_request>
{"id": "confirm_replan", "topic": "重规划方案确认", "prompt": "以上修订方案是否符合预期？", "options": [{"id": "yes", "label": "确认修订"}, {"id": "modify", "label": "我要调整"}], "allow_free_text": true}
</a2ui_request>"""


# Marker appended to a persisted assistant message that delivered a structured
# proposal as a file. The frontend parses it on restore to render a clickable
# proposal.json chip instead of the raw JSON.
_PROPOSAL_FILE_MARKER = "<proposal_file>proposal.json</proposal_file>"


# Richer planner state field groups (freeze contract: Planner memory schema).
# These drive _new_memory() defaults AND the tolerant per-field merge in ws.py
# (a field's group decides its safe-default shape). Listed here once so prompt /
# merge / persistence stay on the same protocol (design D1/D4).
_MEMORY_LIST_FIELDS = (
    "confirmed_constraints", "user_feedback",  # existing
    "open_questions", "assumptions", "non_goals", "success_metrics",
    "decisions_pending", "decisions_confirmed", "tradeoffs", "risks",
    # selected_skills is the only persisted skill tier (user_selected). The
    # other two tiers (default_skills / effective_skills) are DERIVED at
    # resolution time by resolve_effective_skills() from the backend-authoritative
    # DEFAULT_BUILTIN_SKILLS / WORKSPACE_DEFAULT_SKILLS — never persisted (D1/D5).
    "selected_skills",  # existing — user_selected tier
)
_MEMORY_STR_FIELDS = (
    "requirement_summary", "task_classification", "latest_proposal_summary",  # existing
    "architecture_pattern",
)
_MEMORY_STRATEGY_FIELDS = (
    "runtime_strategy", "memory_strategy", "knowledge_strategy", "evaluation_strategy",
)


def _default_apply_readiness() -> dict:
    """Safe default for the apply_readiness sub-structure (status not_ready)."""
    return {"status": "not_ready", "missing": [], "recommendation": ""}


# Fields the LLM's <memory_update> is allowed to drive. selected_skills is
# excluded on purpose — it is managed by attach/detach, not the model.
_MEMORY_MERGEABLE_FIELDS = frozenset(
    set(_MEMORY_STR_FIELDS)
    | (set(_MEMORY_LIST_FIELDS) - {"selected_skills"})
    | set(_MEMORY_STRATEGY_FIELDS)
    | {"apply_readiness"}
)


def _merge_memory_update(memory: dict, update: dict) -> dict:
    """Tolerantly merge a parsed <memory_update> into ``memory`` in place (D4).

    - Only known richer-state fields merge; unknown keys are ignored.
    - Missing fields keep their old value (only produced keys are iterated).
    - Empty / falsy values count as "not produced this turn" and keep the old
      value, so a partial update never wipes accumulated state.
    - Light type-guarding: list fields take lists, strategy/readiness take
      dicts, string fields are coerced to str. A malformed individual field is
      skipped rather than raising — one bad field must not drop the whole turn.
    Returns the same ``memory`` dict for convenience.
    """
    if not isinstance(update, dict):
        return memory
    for k, v in update.items():
        if k not in _MEMORY_MERGEABLE_FIELDS:
            continue
        try:
            if k in _MEMORY_STRATEGY_FIELDS:
                if isinstance(v, dict) and v:
                    memory[k] = v
            elif k == "apply_readiness":
                if isinstance(v, dict) and v:
                    merged = _default_apply_readiness()
                    merged.update(v)
                    memory[k] = merged
            elif k in _MEMORY_STR_FIELDS:
                if isinstance(v, str) and v.strip():
                    memory[k] = v
                elif v not in (None, "", [], {}) and not isinstance(v, (list, dict)):
                    memory[k] = str(v)
            else:  # list fields
                if isinstance(v, list) and v:
                    memory[k] = v
        except Exception:
            continue
    return memory


def _new_memory() -> dict:
    """Default richer planner state — 6 legacy fields + 14 new ones (freeze
    contract). All optional-first with safe defaults: list fields ``[]``,
    strategy fields ``{}``, architecture_pattern ``""``, apply_readiness
    status ``"not_ready"`` (spec: 新建会话初始化 richer state)."""
    mem: dict = {}
    for k in _MEMORY_STR_FIELDS:
        mem[k] = ""
    for k in _MEMORY_LIST_FIELDS:
        mem[k] = []
    for k in _MEMORY_STRATEGY_FIELDS:
        mem[k] = {}
    mem["apply_readiness"] = _default_apply_readiness()
    return mem


def _normalize_pending_a2ui(payload: dict) -> dict:
    """Normalize an a2ui_request payload into a stable ``pending_a2ui`` shape.

    Guarantees the keys the decision-ledger path depends on (id / topic /
    prompt / options / allow_free_text) always exist, so a2ui_response can build
    a complete decision entry regardless of which optional fields the model
    emitted (task 3.2)."""
    payload = payload if isinstance(payload, dict) else {}
    options = payload.get("options")
    options = options if isinstance(options, list) else []
    prompt = str(payload.get("prompt") or "")
    return {
        "id": str(payload.get("id") or ""),
        "topic": str(payload.get("topic") or prompt),
        "prompt": prompt,
        "options": options,
        "allow_free_text": bool(payload.get("allow_free_text", False)),
    }


def _build_decision_entry(
    pending: dict,
    choice_id: str,
    choice_label: str,
    free_text: str = "",
) -> dict:
    """Build one structured decision-ledger entry from a resolved A2UI response
    (design D6). The choice lives here authoritatively; only the free-text
    correction is mirrored to user_feedback, never the choice label."""
    pending = pending if isinstance(pending, dict) else {}
    prompt = str(pending.get("prompt") or "")
    return {
        "id": str(pending.get("id") or "") or f"decision_{abs(hash((prompt, choice_id))) % 100000}",
        "topic": str(pending.get("topic") or prompt),
        "prompt": prompt,
        "options": pending.get("options") or [],
        "selected_option": {"id": choice_id, "label": choice_label},
        "free_text": free_text or "",
        "status": "confirmed",
        "effect_on_plan": "",
    }
