"""Default dimension templates per suite type.

These are the dimension presets the frontend pre-fills into a suite's
``default_dimensions_json`` when a user picks a ``suite_type``. They are plain
code constants (not seeded into the DB) so they can evolve without a migration;
DB-backed user-customizable templates are deferred to a later phase.

Each template is a ``{"dimensions": [...]}`` dict whose dimensions use the same
schema scorers consume: ``{name, type, weight, threshold, required, ...}``.
Weights within a template sum to 1.0, but the engine normalizes anyway.
"""

from __future__ import annotations

import copy
from typing import Any, Dict

# general: deterministic backbone + a judge for overall quality.
GENERAL_TEMPLATE: Dict[str, Any] = {
    "dimensions": [
        {"name": "keyword_coverage", "type": "keyword", "weight": 0.3,
         "threshold": 0.6, "required": False},
        {"name": "format_compliance", "type": "rule", "weight": 0.3,
         "threshold": 0.6, "required": False},
        {"name": "overall_quality", "type": "judge", "weight": 0.4,
         "threshold": 0.6, "required": False,
         "judge_prompt": "评估输出是否准确、完整地回应了用户的请求。"},
    ]
}

# planner: goal completion (required) carries the most weight, plus structure
# and relevancy.
PLANNER_TEMPLATE: Dict[str, Any] = {
    "dimensions": [
        {"name": "goal_completion", "type": "judge", "weight": 0.5,
         "threshold": 0.6, "required": True,
         "judge_prompt": "评估规划方案是否完整覆盖了用户目标，步骤是否合理、可执行。"},
        {"name": "format_compliance", "type": "rule", "weight": 0.25,
         "threshold": 0.6, "required": False},
        {"name": "answer_relevancy", "type": "ragas", "weight": 0.25,
         "threshold": 0.6, "required": False, "metric": "answer_relevancy"},
    ]
}

# rag: retrieval-aware quality. faithfulness (answer grounded in retrieved
# context) is required; context_precision + answer_relevancy round it out, on a
# light keyword/rule backbone. Ragas metrics skip gracefully when ragas is
# absent or contexts are missing, so the keyword/rule dims still produce a score.
RAG_TEMPLATE: Dict[str, Any] = {
    "dimensions": [
        {"name": "keyword_coverage", "type": "keyword", "weight": 0.15,
         "threshold": 0.6, "required": False},
        {"name": "format_compliance", "type": "rule", "weight": 0.15,
         "threshold": 0.6, "required": False},
        {"name": "faithfulness", "type": "faithfulness", "weight": 0.3,
         "threshold": 0.7, "required": True, "metric": "faithfulness"},
        {"name": "context_precision", "type": "context_precision", "weight": 0.2,
         "threshold": 0.6, "required": False, "metric": "context_precision"},
        {"name": "answer_relevancy", "type": "answer_relevancy", "weight": 0.2,
         "threshold": 0.6, "required": False, "metric": "answer_relevancy"},
    ]
}

# workflow: multi-step DAG agents. goal_completion (judge) for the outcome, plus
# trace-aware structural checks on the execution trajectory (no error steps,
# step count within an expected band). The trace dims skip on a non-DAG / empty
# trajectory, so this never penalizes runs that produced no trace.
WORKFLOW_TEMPLATE: Dict[str, Any] = {
    "dimensions": [
        {"name": "goal_completion", "type": "judge", "weight": 0.5,
         "threshold": 0.6, "required": True,
         "judge_prompt": "评估智能体是否达成了用户目标，最终结果是否正确、完整。"},
        {"name": "no_error_steps", "type": "trace", "weight": 0.3,
         "threshold": 0.6, "required": True, "check": "no_error_steps"},
        {"name": "step_count_range", "type": "trace", "weight": 0.2,
         "threshold": 0.6, "required": False, "check": "step_count_range",
         "min_steps": 1, "max_steps": 20},
    ]
}

TEMPLATES: Dict[str, Dict[str, Any]] = {
    "general": GENERAL_TEMPLATE,
    "planner": PLANNER_TEMPLATE,
    "rag": RAG_TEMPLATE,
    "workflow": WORKFLOW_TEMPLATE,
}


def get_template(suite_type: str) -> Dict[str, Any]:
    """Return a deep copy of the template for ``suite_type``.

    Unknown types fall back to the general template. A copy is returned so
    callers can mutate it without corrupting the module constants.
    """
    return copy.deepcopy(TEMPLATES.get(suite_type, GENERAL_TEMPLATE))
