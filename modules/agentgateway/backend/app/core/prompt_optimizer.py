"""Prompt Optimizer Agent — analyzes and improves prompts based on test conversation context."""

import json
from typing import AsyncIterator, Tuple

OPTIMIZER_SYSTEM_PROMPT = """你是一个提示词优化专家。你的任务是分析和改进智能体的系统提示词。

当收到当前提示词和测试对话记录后，你需要：

1. 分析当前提示词的不足之处：
   - 是否清晰定义了角色和职责？
   - 是否包含足够的约束和边界？
   - 输出格式是否有明确要求？
   - 是否考虑了异常情况处理？

2. 根据测试对话中暴露的问题，针对性改进：
   - 回复含糊的地方 → 增加更具体的指令
   - 回答错误的地方 → 增加事实约束
   - 格式不一致 → 增加格式规范
   - 角色偏离 → 强化角色描述

3. 输出优化后的完整提示词，以及变更列表。

请以以下 JSON 格式返回结果：

```json
{
  "optimized_prompt": {
    "role_name": "...",
    "role_description": "...",
    "system_prompt": "..."
  },
  "changes": [
    {
      "field": "system_prompt",
      "before": "原文片段...",
      "after": "修改后片段...",
      "reason": "修改原因"
    }
  ]
}
```

优化原则：
- 保持原有提示词的核心意图不变
- 每次优化聚焦 2-5 个关键改进点
- 修改要有针对性，基于对话中实际暴露的问题
- 不要过度优化导致提示词过于冗长"""


async def optimize_prompt(
    current_prompt: dict,
    recent_messages: list[dict],
) -> dict:
    """Analyze and optimize a prompt based on test conversation context.

    Args:
        current_prompt: Current prompt config dict with role_name, role_description, system_prompt
        recent_messages: List of {role, content} dicts from test conversation

    Returns:
        dict with optimized_prompt and changes array
    """
    from app.core.agentscope_runner import create_agent
    from agentscope.message import UserMsg

    messages_text = "\n".join(
        f"[{m['role']}]: {m['content'][:200]}" for m in recent_messages[-6:]
    ) if recent_messages else "（无测试对话记录）"

    user_content = (
        f"当前提示词配置：\n"
        f"role_name: {current_prompt.get('role_name', '')}\n"
        f"role_description: {current_prompt.get('role_description', '')}\n"
        f"system_prompt: {current_prompt.get('system_prompt', '')}\n\n"
        f"最近的测试对话记录：\n{messages_text}\n\n"
        f"请分析并优化这个提示词，以 JSON 格式返回结果。"
    )

    agent = create_agent(
        system_prompt=OPTIMIZER_SYSTEM_PROMPT,
        model_name="glm-4-flash",
        provider="glm",
        stream=False,
    )

    msg = UserMsg(name="user", content=user_content)
    response = await agent.reply(msg)
    text = response.get_text_content() or ""

    # Extract JSON from response (handle markdown code blocks)
    json_text = text
    if "```json" in text:
        json_text = text.split("```json")[1].split("```")[0].strip()
    elif "```" in text:
        json_text = text.split("```")[1].split("```")[0].strip()

    try:
        result = json.loads(json_text)
    except json.JSONDecodeError:
        # Fallback: return raw text as optimized_prompt
        result = {
            "optimized_prompt": {
                "role_name": current_prompt.get("role_name", ""),
                "role_description": current_prompt.get("role_description", ""),
                "system_prompt": text,
            },
            "changes": [{"field": "system_prompt", "before": "", "after": text, "reason": "优化器返回了非结构化结果"}],
        }

    return result
