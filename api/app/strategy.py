"""Strategy Agent（PRD §3.2 / §6.4）—— M2 任务规划层。

输入用户请求 + 当前可用能力（知识库 / 连接器 / 智能体身份），输出结构化 JSON plan：
一句话目标、是否需要检索、是否需要外部工具、拆解步骤。M2 步骤类型限定
retrieve / respond / tool（skill 步骤 M3 接入；tool 步骤 M2 仍在 respond 的工具循环里执行，
此处只做预测，M4 由 MCP Strategy Agent 落成可执行步骤）。

LLM 不可用（无 key）或返回非法 JSON 时，退化为确定性默认计划——与 M1 行为一致：
有知识库就先检索再回答，否则直接回答。
"""
import json

from .model_gateway import complete

_ALLOWED_TYPES = {"retrieve", "respond", "skill", "tool"}
_EXECUTOR_BY_TYPE = {"retrieve": "rag", "respond": "llm", "skill": "skill_agent", "tool": "mcp_tool"}
_DEFAULT_TITLE = {"retrieve": "检索知识库", "respond": "生成回答", "skill": "执行技能", "tool": "调用工具"}

PLANNER_SYSTEM = """你是 Atlas 智能体平台的任务规划器（Strategy Agent）。
根据用户请求和当前可用能力，输出一个 JSON 执行计划，用于指导后续执行。

严格要求：
- 只输出 JSON 本体，不要任何解释文字，不要 markdown 代码块。
- 顶层字段：goal(字符串，一句话目标)、requires_knowledge(布尔)、requires_tools(布尔)、steps(数组)。
- requires_knowledge：仅当用户问题确实需要参考企业知识库资料才设为 true；
  闲聊、问候、常识、纯写作、翻译、代码等不需要检索的，一律设为 false。
- requires_tools：用户请求涉及搜索、查询网页/地图/论文/社交媒体/外部实时数据时，只要有已启用的连接器就必须设为 true。
  对以下类型请求尤其要设为 true：小红书搜索、网页内容抓取、地图查询、论文检索、Excel 操作。
- steps 每项含：id、type(取值 retrieve|respond|tool)、title(中文短语)、executor(取值 rag|llm|mcp_tool)。
- 有 requires_tools=true 时，必须包含至少一个 tool 步骤（放在 respond 之前），
  tool 步骤的 connector 字段填对应的连接器 ID。
- 通常以一个 respond 步骤收尾；需要检索时 retrieve 步骤放在最前；需要工具时 tool 步骤放在 respond 前。
- skills：从下方「可用技能」列表里选出最适合本次任务的技能 id（0..N 个，可为空数组），
  放进 steps 同级的 "skills" 字段。只填列表中真实存在的 id；不确定就留空数组。
"""


def _user_prompt(input_text: str, has_kb: bool, connectors: list[str], agent_prompt: str | None,
                 skill_catalog: list[dict] | None = None) -> str:
    lines = [
        f"用户请求：{input_text}",
        f"当前可用知识库：{'有' if has_kb else '无'}",
        f"已启用连接器：{', '.join(connectors) if connectors else '无'}",
    ]
    if agent_prompt:
        lines.append(f"智能体身份与规则：{agent_prompt[:300]}")
    if skill_catalog:
        lines.append("可用技能（仅元数据；不要请求或推断未选中技能的正文）：")
        for s in skill_catalog:
            desc = (s.get("description") or "").strip()
            summary = (s.get("summary") or "").strip()
            line = f"- {s['id']} — {s['name']}"
            if desc:
                line += f"：{desc}"
            if summary:
                line += f"｜摘要：{summary}"
            lines.append(line)
    lines.append("请输出 JSON 计划。")
    return "\n".join(lines)


def _parse_json(raw: str) -> dict | None:
    """容错解析：先直接 parse，失败再抠出第一个 {...} 段（有的模型会包代码块/前后缀）。"""
    if not raw:
        return None
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception:
        pass
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end > start:
        try:
            data = json.loads(raw[start:end + 1])
            return data if isinstance(data, dict) else None
        except Exception:
            return None
    return None


def _normalize(data: dict, has_kb: bool, connectors: list[str], input_text: str,
               skill_catalog: list[dict] | None = None) -> dict:
    requires_knowledge = bool(data.get("requires_knowledge")) and has_kb
    requires_tools = bool(data.get("requires_tools")) and bool(connectors)
    steps: list[dict] = []
    for i, raw_step in enumerate(data.get("steps") or []):
        if not isinstance(raw_step, dict) or len(steps) >= 6:
            continue
        t = raw_step.get("type")
        if t not in _ALLOWED_TYPES:
            t = "respond"
        if t == "retrieve" and not has_kb:
            continue
        if t == "tool" and not connectors:
            continue
        if t == "skill":  # M3 才接入 skill，M2 一律按普通回答处理
            t = "respond"
        step = {
            "id": str(raw_step.get("id") or f"step_{len(steps) + 1}"),
            "type": t,
            "title": str(raw_step.get("title") or _DEFAULT_TITLE[t])[:120],
            "executor": _EXECUTOR_BY_TYPE[t],
        }
        if t == "tool" and raw_step.get("connector"):
            step["connector"] = str(raw_step["connector"])
        if raw_step.get("risk") in ("read", "write"):
            step["risk"] = raw_step["risk"]
        # 合并连续的 respond（skill 在 M2 降级为 respond，可能与既有 respond 撞在一起）
        if step["type"] == "respond" and steps and steps[-1]["type"] == "respond":
            continue
        steps.append(step)
    # 计划必须以 respond 收尾，保证一定有最终回答
    if not any(s["type"] == "respond" for s in steps):
        steps.append({"id": f"step_{len(steps) + 1}", "type": "respond", "title": "生成回答", "executor": "llm"})
    # requires_knowledge 与 steps 对齐：要检索但计划没列 retrieve，则补在最前
    if requires_knowledge and not any(s["type"] == "retrieve" for s in steps):
        steps.insert(0, {"id": "step_0", "type": "retrieve", "title": "检索知识库", "executor": "rag"})
    if not requires_knowledge:
        steps = [s for s in steps if s["type"] != "retrieve"]
    # skill 自动选：仅保留 catalog 内真实存在的 id
    catalog_ids = {s["id"] for s in (skill_catalog or [])}
    skills = [sid for sid in (data.get("skills") or []) if sid in catalog_ids]
    return {
        "goal": str(data.get("goal") or input_text[:60]),
        "requires_knowledge": requires_knowledge,
        "requires_tools": requires_tools,
        "steps": steps,
        "skills": skills,
        "source": "llm",
    }


def fallback_plan(input_text: str, has_kb: bool, connectors: list[str]) -> dict:
    """无 LLM / 解析失败时的确定性计划：有连接器则优先工具调用。"""
    steps: list[dict] = []
    if has_kb:
        steps.append({"id": "step_1", "type": "retrieve", "title": "检索知识库", "executor": "rag"})
    if connectors:
        steps.append({"id": f"step_{len(steps) + 1}", "type": "tool", "title": "调用外部工具", "executor": "mcp_tool"})
    steps.append({"id": f"step_{len(steps) + 1}", "type": "respond", "title": "生成回答", "executor": "llm"})
    return {
        "goal": input_text[:60],
        "requires_knowledge": has_kb,
        "requires_tools": bool(connectors),
        "steps": steps,
        "skills": [],
        "source": "fallback",
    }


async def plan(*, input_text: str, has_knowledge_base: bool, enabled_connectors: list[str],
               agent_prompt: str | None, model: str | None,
               skill_catalog: list[dict] | None = None) -> dict:
    """让 Strategy Agent 产出结构化计划（含自动选 skill）；任何异常都退化到确定性计划，绝不阻断主流程。"""
    fallback = fallback_plan(input_text, has_knowledge_base, enabled_connectors)
    try:
        raw = await complete(
            [
                {"role": "system", "content": PLANNER_SYSTEM},
                {"role": "user", "content": _user_prompt(input_text, has_knowledge_base, enabled_connectors, agent_prompt, skill_catalog)},
            ],
            model,
            temperature=0,
            max_tokens=500,
        )
        data = _parse_json(raw)
        if not data:
            return fallback
        return _normalize(data, has_knowledge_base, enabled_connectors, input_text, skill_catalog)
    except Exception:
        return fallback
