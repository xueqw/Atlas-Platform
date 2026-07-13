"""评测结果分析（PRD §5.6）：失败原因归类、修复建议、总状态判断。

纯函数，不碰数据库/网络，方便直接单测。这里的判断都是确定性规则，不涉及语义理解——
PRD 里"任务完成率/引用准确率/幻觉率"这类需要模型判断的指标本次不做，避免用规则伪造语义结论。
"""


def classify_failure(ok: bool, error: str | None, expected: str, logs: str) -> str:
    """失败原因归类。ok=True 时无意义，调用方应只在失败样例上调用。"""
    if error:
        return "exit_error"
    if expected and expected not in logs:
        return "output_mismatch"
    return "unknown"


_SUGGESTIONS = {
    "exit_error": "检查 main() 是否抛出异常，或模型/沙箱调用是否报错——查看日志里的 stderr 或 ERROR 行定位。",
    "output_mismatch": "实际输出没有包含期望内容，检查 main() 的返回值逻辑，或调整这条样例的期望文本。",
    "unknown": "暂无法自动判断具体原因，建议查看完整运行日志人工排查。",
}


def suggest_fix(failure_reason: str) -> str:
    return _SUGGESTIONS.get(failure_reason, _SUGGESTIONS["unknown"])


def build_evaluation_summary(pass_rate: float, avg_elapsed_ms: float,
                              declared_skills: list[str], declared_connectors: list[str],
                              results: list[dict] | None = None) -> dict:
    """总状态分档：这是本次设定的简单启发式阈值（通过率=100% 可发布，>=70% 建议优化后发布，
    否则暂不建议），不是对回答质量的语义判断，仅反映确定性测试样例的通过情况。"""
    if pass_rate >= 1.0:
        recommendation, label = "publish", "可以发布"
    elif pass_rate >= 0.7:
        recommendation, label = "optimize", "建议优化后发布"
    else:
        recommendation, label = "hold", "暂不建议发布"

    result_items = results or []
    tool_calls = [call for item in result_items for call in (item.get("tool_calls") or [])]
    skill_traces = [trace for item in result_items for trace in (item.get("trace") or []) if trace.get("type") == "skill"]
    tool_success_rate = (
        sum(1 for call in tool_calls if call.get("status") == "succeeded") / len(tool_calls)
        if tool_calls else None
    )
    skill_success_rate = (
        sum(1 for trace in skill_traces if trace.get("status") == "succeeded") / len(skill_traces)
        if skill_traces else None
    )
    return {
        "recommendation": recommendation,
        "recommendation_label": label,
        "avg_elapsed_ms": round(avg_elapsed_ms, 1),
        "declared_skills": declared_skills,
        "declared_connectors": declared_connectors,
        "metrics": {
            "task_completion_rate": round(pass_rate, 4),
            "first_pass_rate": round(pass_rate, 4),
            "citation_accuracy": None,
            "hallucination_rate": None,
            "skill_success_rate": round(skill_success_rate, 4) if skill_success_rate is not None else None,
            "tool_success_rate": round(tool_success_rate, 4) if tool_success_rate is not None else None,
            "avg_elapsed_ms": round(avg_elapsed_ms, 1),
            "token_cost": None,
        },
        "metric_notes": {
            "citation_accuracy": "需要带标准答案与引用标注的评测集后计算",
            "hallucination_rate": "需要配置评测模型后计算，当前不伪造语义指标",
            "token_cost": "模型供应商返回 usage 后计算",
        },
    }
