"""LLM-as-judge scorer.

Scores subjective dimensions (goal completion, helpfulness, tone…) by asking
an LLM to grade the output against free-text criteria. The judge is a
*supplement*: when it is unavailable or returns an unparseable reply, the
dimension is skipped (not failed) so a missing judge never sinks a case.

The judge LLM is fixed to JUDGE_PROVIDER/JUDGE_MODEL (openai/gpt-5.4), reused
across the system to keep the config surface small.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional, Tuple

from app.core.scorers.base import DimensionResult, ScoreContext, register, skipped_result

_log = logging.getLogger(__name__)

JUDGE_PROVIDER = "openai"
JUDGE_MODEL = "gpt-5.4"


async def _llm_judge(judge_prompt: str, user_input: str,
                     output: str) -> Optional[Tuple[float, str]]:
    """Grade an output 0..1 against free-text criteria via an LLM judge.

    Returns ``(score, reason)`` or ``None`` when the judge is unavailable or
    its reply can't be parsed, so the caller can skip the dimension rather than
    fail the case.
    """
    try:
        from app.core.agentscope_runner import create_agent, run_conversation

        system_prompt = (
            "你是一个严格的评估员。根据给定的评分标准，对智能体输出打分。"
            "只返回一个 JSON 对象：{\"score\": 0.0-1.0, \"reason\": \"...\"}，"
            "score 为 0 到 1 的小数，1 表示完全满足标准。"
        )
        user_content = (
            f"评分标准：\n{judge_prompt}\n\n"
            f"用户输入：\n{user_input}\n\n"
            f"智能体输出：\n{output}\n\n"
            f"请打分并以 JSON 返回。"
        )
        # Stream the judge. Some OpenAI-compatible gateways (e.g. aizzz.org) force
        # an SSE stream for gpt-5 reasoning models even when stream=False, which
        # makes the non-streaming agent.reply() path choke with
        # "'str' object has no attribute 'choices'". run_conversation always
        # consumes the stream, so we accumulate its tokens into the full reply.
        agent = create_agent(
            system_prompt=system_prompt,
            model_name=JUDGE_MODEL,
            provider=JUDGE_PROVIDER,
            stream=True,
        )
        parts = []
        async for ev_type, content in run_conversation(agent, user_content):
            if ev_type == "token" and content:
                parts.append(content)
        text = "".join(parts)
    except Exception as exc:
        _log.warning("llm_judge unavailable: %s: %s", type(exc).__name__, exc)
        return None

    json_text = text
    if "```json" in text:
        json_text = text.split("```json")[1].split("```")[0].strip()
    elif "```" in text:
        json_text = text.split("```")[1].split("```")[0].strip()
    try:
        data = json.loads(json_text)
        score = max(0.0, min(1.0, float(data.get("score"))))
        reason = str(data.get("reason", "")) or "LLM judge 评分"
        return score, reason
    except (json.JSONDecodeError, TypeError, ValueError):
        _log.warning("llm_judge returned unparseable output")
        return None


async def judge_scorer(dim_cfg: Dict[str, Any], ctx: ScoreContext) -> DimensionResult:
    """Score one judge dimension; skip (not fail) when the judge is unavailable.

    The grading criteria come from the dimension config (``judge_prompt`` /
    ``prompt`` / ``criteria``) or the case metadata's ``judge_prompt`` (set by
    the legacy mapper).
    """
    try:
        threshold = max(0.0, min(1.0, float(dim_cfg.get("threshold", 0.6))))
    except (TypeError, ValueError):
        threshold = 0.6

    prompt = (dim_cfg.get("judge_prompt") or dim_cfg.get("prompt")
              or dim_cfg.get("criteria") or ctx.metadata.get("judge_prompt", ""))
    prompt = (prompt or "").strip()
    if not prompt:
        return skipped_result(dim_cfg, "未提供评分标准 (judge_prompt)，跳过该维度")

    result = await _llm_judge(prompt, ctx.user_input, ctx.output)
    if result is None:
        return skipped_result(dim_cfg, "LLM judge 不可用或返回无法解析，跳过该维度")

    score, reason = result
    name = str(dim_cfg.get("name", "judge"))
    return DimensionResult(
        dimension=name,
        type=str(dim_cfg.get("type", "judge")),
        score=score,
        passed=score >= threshold,
        threshold=threshold,
        weight=float(dim_cfg.get("weight", 0.0) or 0.0),
        required=bool(dim_cfg.get("required", False)),
        reason=reason,
        evidence={"judge_prompt": prompt, "score": score},
    )


register("judge", judge_scorer)
