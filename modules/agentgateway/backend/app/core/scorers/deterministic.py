"""Deterministic scorers — keyword, schema, rule.

These need no LLM, so they're the reliable backbone of a case's score. The
keyword and schema logic is migrated verbatim from the legacy ``score_case``;
``rule`` is new and covers contains/not_contains/regex/length style checks
declared in the dimension config (or pulled from the case's constraints).
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List

import jsonschema

from app.core.scorers.base import DimensionResult, ScoreContext, register


def _coerce_threshold(dim_cfg: Dict[str, Any], default: float = 0.6) -> float:
    try:
        t = float(dim_cfg.get("threshold", default))
    except (TypeError, ValueError):
        t = default
    return max(0.0, min(1.0, t))


def _common(dim_cfg: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "dimension": str(dim_cfg.get("name", dim_cfg.get("type", "dimension"))),
        "type": str(dim_cfg.get("type", "")),
        "weight": float(dim_cfg.get("weight", 0.0) or 0.0),
        "required": bool(dim_cfg.get("required", False)),
    }


async def keyword_scorer(dim_cfg: Dict[str, Any], ctx: ScoreContext) -> DimensionResult:
    """Hit-rate of expected keywords found (case-insensitive) in the output.

    Keywords come from the dimension config (``keywords``) or fall back to the
    case metadata's ``expected_keywords`` (set by the legacy mapper).
    """
    threshold = _coerce_threshold(dim_cfg)
    keywords = dim_cfg.get("keywords")
    if keywords is None:
        keywords = ctx.metadata.get("expected_keywords", [])
    if isinstance(keywords, str):
        try:
            keywords = json.loads(keywords or "[]")
        except json.JSONDecodeError:
            keywords = []
    keywords = [str(k) for k in (keywords or [])]

    lower_output = (ctx.output or "").lower()
    if not keywords:
        return DimensionResult(
            score=1.0, passed=True, threshold=threshold,
            reason="无关键词约束", evidence={"keywords": []}, **_common(dim_cfg),
        )

    hit, missed = [], []
    for kw in keywords:
        (hit if kw.lower() in lower_output else missed).append(kw)
    score = len(hit) / len(keywords)
    return DimensionResult(
        score=score, passed=score >= threshold, threshold=threshold,
        reason=f"命中 {len(hit)}/{len(keywords)} 个关键词",
        evidence={"hit": hit, "missed": missed},
        **_common(dim_cfg),
    )


async def schema_scorer(dim_cfg: Dict[str, Any], ctx: ScoreContext) -> DimensionResult:
    """1.0 when the output parses as JSON validating against the schema, else 0.

    Schema comes from the dimension config (``schema``) or case metadata's
    ``expected_schema`` (legacy mapper).
    """
    threshold = _coerce_threshold(dim_cfg, default=1.0)
    schema = dim_cfg.get("schema")
    if schema is None:
        schema = ctx.metadata.get("expected_schema", {})
    if isinstance(schema, str):
        try:
            schema = json.loads(schema or "{}")
        except json.JSONDecodeError:
            schema = {}

    if not schema:
        return DimensionResult(
            score=1.0, passed=True, threshold=threshold,
            reason="无 schema 约束", evidence={}, **_common(dim_cfg),
        )

    try:
        data = json.loads(ctx.output) if isinstance(ctx.output, str) else ctx.output
        jsonschema.validate(instance=data, schema=schema)
        return DimensionResult(
            score=1.0, passed=True, threshold=threshold,
            reason="输出符合 JSON schema", evidence={"valid": True},
            **_common(dim_cfg),
        )
    except Exception as exc:  # json error or ValidationError
        return DimensionResult(
            score=0.0, passed=False, threshold=threshold,
            reason=f"schema 校验失败: {type(exc).__name__}",
            evidence={"valid": False, "error": str(exc)[:500]},
            **_common(dim_cfg),
        )


def _eval_rule(rule: Dict[str, Any], output: str) -> tuple[bool, str]:
    """Evaluate a single rule against output. Returns (passed, detail)."""
    rtype = str(rule.get("type", "contains"))
    value = rule.get("value", "")
    lower_out = output.lower()

    if rtype == "contains":
        ok = str(value).lower() in lower_out
        return ok, f"contains '{value}': {ok}"
    if rtype == "not_contains":
        ok = str(value).lower() not in lower_out
        return ok, f"not_contains '{value}': {ok}"
    if rtype == "regex":
        try:
            ok = re.search(str(value), output) is not None
        except re.error as exc:
            return False, f"invalid regex: {exc}"
        return ok, f"regex '{value}': {ok}"
    if rtype == "min_length":
        try:
            ok = len(output) >= int(value)
        except (TypeError, ValueError):
            return False, "min_length: invalid value"
        return ok, f"min_length {value}: len={len(output)}"
    if rtype == "max_length":
        try:
            ok = len(output) <= int(value)
        except (TypeError, ValueError):
            return False, "max_length: invalid value"
        return ok, f"max_length {value}: len={len(output)}"
    if rtype == "non_empty":
        ok = bool(output.strip())
        return ok, f"non_empty: {ok}"
    return False, f"unknown rule type '{rtype}'"


async def rule_scorer(dim_cfg: Dict[str, Any], ctx: ScoreContext) -> DimensionResult:
    """Fraction of declared rules that pass.

    Rules come from the dimension config ``rules`` (a list of
    ``{type, value}``) or fall back to the case constraints' ``rules``.
    Supported types: contains / not_contains / regex / min_length /
    max_length / non_empty.
    """
    threshold = _coerce_threshold(dim_cfg)
    rules: List[Dict[str, Any]] = dim_cfg.get("rules")
    if rules is None:
        rules = ctx.constraints.get("rules", [])
    if not isinstance(rules, list):
        rules = []

    if not rules:
        return DimensionResult(
            score=1.0, passed=True, threshold=threshold,
            reason="无规则约束", evidence={"rules": []}, **_common(dim_cfg),
        )

    output = ctx.output or ""
    details, passed_count = [], 0
    for rule in rules:
        if not isinstance(rule, dict):
            details.append({"rule": rule, "passed": False, "detail": "malformed rule"})
            continue
        ok, detail = _eval_rule(rule, output)
        passed_count += 1 if ok else 0
        details.append({"rule": rule, "passed": ok, "detail": detail})

    score = passed_count / len(rules)
    return DimensionResult(
        score=score, passed=score >= threshold, threshold=threshold,
        reason=f"满足 {passed_count}/{len(rules)} 条规则",
        evidence={"rules": details},
        **_common(dim_cfg),
    )


register("keyword", keyword_scorer)
register("schema", schema_scorer)
register("rule", rule_scorer)
