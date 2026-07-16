"""Builder Agent — multi-turn conversational wizard that helps users design agents.

The agent interviews the user to discover requirements, then queries the capability
library to recommend a prompt template, model, and tools as structured JSON.
"""

import json
from typing import AsyncIterator, Tuple
from sqlmodel import Session, select

from app.core.database import engine
from app.models.db import CapabilityItem


BUILDER_SYSTEM_PROMPT = """你是一个智能体构建向导。你的任务是通过多轮对话，帮助用户设计一个智能体。

你需要逐步了解：
1. 用户想要构建什么类型的智能体？（客服、技术支持、代码助手、数据分析等）
2. 智能体需要什么样的角色定位？（专业严谨、友好亲切、幽默风趣）
3. 智能体需要处理什么类型的任务？
4. 有什么特殊约束或要求？

在对话过程中：
- 每次只问 1-2 个问题，不要一次问太多
- 当用户表达不清晰时，用具体例子引导
- 收集足够信息后，告诉用户你准备生成推荐配置

当你收集到足够信息后，输出一个 JSON 推荐配置：

```json
{
  "ready": true,
  "summary": "基于你的需求，我推荐...",
  "recommendation": {
    "prompt": {
      "role_name": "...",
      "role_description": "...",
      "system_prompt": "..."
    },
    "model": {
      "provider": "...",
      "model_name": "...",
      "reason": "..."
    },
    "tools": [
      {"name": "...", "reason": "..."}
    ],
    "dag": {
      "nodes": [
        {"type": "p", "label": "提示词"},
        {"type": "m", "label": "模型"},
        {"type": "t", "label": "工具"}
      ],
      "edges": [
        {"from": "p", "to": "m"},
        {"from": "m", "to": "t"}
      ]
    }
  }
}
```

dag 字段是可选的，表示推荐的 DAG 拓扑结构。nodes 中的 type 可用值: p(提示词), m(模型), i(入口), o(出口), k(知识库), t(工具), c(条件), x(代码), h(HTTP), a(审批)。edges 中的 from/to 指向 nodes 数组中的索引。大多数场景使用 p→m 即可，只有明确需要知识库、工具、条件分支等场景才添加额外节点。

不要直接输出能力库中的 config JSON，而是根据对话内容生成自然、有针对性的推荐。
如果能力库中没有完美匹配的选项，选择最接近的并说明原因。"""


def _search_capabilities(keyword: str) -> list[dict]:
    """Search capability items by keyword."""
    with Session(engine) as session:
        rows = session.exec(select(CapabilityItem)).all()
        kw = keyword.lower()
        results = [
            {"type": r.type, "name": r.name, "description": r.description, "config": r.config}
            for r in rows
            if kw in r.name.lower() or kw in r.description.lower() or kw in r.tags.lower()
        ]
        return results


def _get_all_capabilities() -> dict:
    """Return all capabilities grouped by type."""
    with Session(engine) as session:
        rows = session.exec(select(CapabilityItem)).all()
        result = {"prompt": [], "model": [], "tool": []}
        for r in rows:
            result.get(r.type, []).append({
                "name": r.name,
                "description": r.description,
                "config": r.config,
                "tags": r.tags,
            })
        return result


async def run_builder_conversation(agent, user_message: str, conversation_history: list) -> AsyncIterator[Tuple[str, str]]:
    """Run one turn of builder conversation. Yields (type, content) tuples."""
    from agentscope.message import UserMsg

    yield ("thinking", "构建向导正在思考...")

    enhanced_message = user_message

    # If the user mentions keywords related to recommendation, inject capability context
    if any(kw in user_message for kw in ["推荐", "建议", "配置", "用什么", "选什么"]):
        caps = _get_all_capabilities()
        available = []
        for cap_type, items in caps.items():
            names = [item["name"] for item in items]
            available.append(f"- {cap_type}: {', '.join(names)}")
        enhanced_message = (
            f"{user_message}\n\n"
            f"[系统提示：以下是当前能力库中可用的资源]\n" + "\n".join(available)
        )

    msg = UserMsg(name="user", content=enhanced_message)
    response = await agent.reply(msg)
    text = response.get_text_content() or ""

    yield ("token", text)
    yield ("done", "")