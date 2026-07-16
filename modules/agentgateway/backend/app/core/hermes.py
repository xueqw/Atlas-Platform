"""Hermes Rule Engine — 4-layer validation system for DAG agents.

Layers:
1. Parameter Validation — node config against config_schema JSON Schema
2. Connection Constraints — DAG topology rules (no cycles, valid connections)
3. Build Evaluation — test-driven quality scoring
4. Runtime Monitoring — production quality degradation detection
"""

import json
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

import jsonschema

from app.core.dag_executor import DAGParser, StateManager, DAGRunner


@dataclass
class ValidationError:
    node_id: str
    node_type: str
    rule_type: str  # param_validation, connection_constraint
    severity: str  # error, warning
    message: str


@dataclass
class ValidationReport:
    errors: List[ValidationError] = field(default_factory=list)
    warnings: List[ValidationError] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return len(self.errors) == 0

    @property
    def blocking_errors(self) -> List[ValidationError]:
        return [e for e in self.errors if e.severity == "error"]

    @property
    def non_blocking_warnings(self) -> List[ValidationError]:
        return self.warnings + [e for e in self.errors if e.severity == "warning"]


# ─── Layer 1: Parameter Validation ──────────────────────────────────────────


class ParameterValidator:
    """Validate node configs against their JSON Schema definitions."""

    @staticmethod
    def validate_node(node_id: str, node_type: str, config: Dict[str, Any],
                      config_schema: Optional[str] = None) -> List[ValidationError]:
        errors: List[ValidationError] = []

        if config_schema is None:
            from app.core.nodes.base import NodeRegistry
            node_cls = NodeRegistry.get(node_type)
            if node_cls:
                config_schema = node_cls.config_schema

        if not config_schema:
            return errors

        try:
            schema = json.loads(config_schema) if isinstance(config_schema, str) else config_schema
        except json.JSONDecodeError:
            return [ValidationError(
                node_id=node_id, node_type=node_type,
                rule_type="param_validation", severity="error",
                message=f"Invalid config_schema JSON for node type '{node_type}'",
            )]

        # Validate required fields
        for prop_name in schema.get("required", []):
            if prop_name not in config or config.get(prop_name) in (None, ""):
                prop_title = schema.get("properties", {}).get(prop_name, {}).get("title", prop_name)
                errors.append(ValidationError(
                    node_id=node_id, node_type=node_type,
                    rule_type="param_validation", severity="error",
                    message=f"必填字段 '{prop_title}' 未填写",
                ))

        # Validate with jsonschema
        try:
            jsonschema.validate(instance=config, schema=schema)
        except jsonschema.ValidationError as exc:
            errors.append(ValidationError(
                node_id=node_id, node_type=node_type,
                rule_type="param_validation", severity="error",
                message=f"字段验证失败: {exc.message}",
            ))

        return errors

    @staticmethod
    def validate_dag(parser: DAGParser) -> ValidationReport:
        """Validate all node configs in a parsed DAG."""
        report = ValidationReport()
        for node in parser.nodes:
            node_errors = ParameterValidator.validate_node(
                node.id, node.node_type, node.config,
            )
            for err in node_errors:
                if err.severity == "error":
                    report.errors.append(err)
                else:
                    report.warnings.append(err)
        return report


# ─── Layer 2: Connection Constraints ─────────────────────────────────────────


class ConnectionConstraintEngine:
    """Evaluate DAG topology rules against connection constraints."""

    _global_rules: List[Callable] = []

    @classmethod
    def register_rule(cls, rule_fn: Callable):
        cls._global_rules.append(rule_fn)
        return rule_fn

    @classmethod
    def evaluate(cls, parser: DAGParser) -> ValidationReport:
        report = ValidationReport()
        for rule_fn in cls._global_rules:
            result = rule_fn(parser)
            for err in result:
                if err.severity == "error":
                    report.errors.append(err)
                else:
                    report.warnings.append(err)
        return report


# Built-in connection constraints

def _constraint_i_no_incoming(parser: DAGParser) -> List[ValidationError]:
    errors = []
    for node in parser.nodes:
        if node.node_type == "i":
            incoming = parser.get_incoming_edges(node.id)
            if incoming:
                errors.append(ValidationError(
                    node_id=node.id, node_type="i",
                    rule_type="connection_constraint", severity="error",
                    message="入口节点(I)不能有入边",
                ))
    return errors


def _constraint_o_no_outgoing(parser: DAGParser) -> List[ValidationError]:
    errors = []
    for node in parser.nodes:
        if node.node_type == "o":
            outgoing = parser.get_outgoing_edges(node.id)
            if outgoing:
                errors.append(ValidationError(
                    node_id=node.id, node_type="o",
                    rule_type="connection_constraint", severity="error",
                    message="出口节点(O)不能有出边",
                ))
    return errors


def _constraint_no_cycles(parser: DAGParser) -> List[ValidationError]:
    if parser.has_cycles():
        return [ValidationError(
            node_id="*", node_type="*",
            rule_type="connection_constraint", severity="error",
            message="DAG 图中不能有循环依赖",
        )]
    return []


def _constraint_no_isolated_nodes(parser: DAGParser) -> List[ValidationError]:
    errors = []
    connected = set()
    for edge in parser.edges:
        connected.add(edge.source)
        connected.add(edge.target)
    for node in parser.nodes:
        if node.id not in connected:
            errors.append(ValidationError(
                node_id=node.id, node_type=node.node_type,
                rule_type="connection_constraint", severity="warning",
                message=f"节点 '{node.id}' ({node.node_type}) 是孤立节点",
            ))
    return errors


def _constraint_m_has_prompt_upstream(parser: DAGParser) -> List[ValidationError]:
    errors = []

    def _has_p_upstream(node_id: str, visited: set) -> bool:
        if node_id in visited:
            return False
        visited.add(node_id)
        node = parser._node_map.get(node_id)
        if node and node.node_type == "p":
            return True
        for edge in parser.get_incoming_edges(node_id):
            if _has_p_upstream(edge.source, visited):
                return True
        return False

    for node in parser.nodes:
        if node.node_type == "m":
            if not _has_p_upstream(node.id, set()):
                errors.append(ValidationError(
                    node_id=node.id, node_type="m",
                    rule_type="connection_constraint", severity="error",
                    message="模型节点(M)上游必须有提示词来源(P节点)",
                ))
    return errors


# Register built-in constraints
ConnectionConstraintEngine.register_rule(_constraint_i_no_incoming)
ConnectionConstraintEngine.register_rule(_constraint_o_no_outgoing)
ConnectionConstraintEngine.register_rule(_constraint_no_cycles)
ConnectionConstraintEngine.register_rule(_constraint_no_isolated_nodes)
ConnectionConstraintEngine.register_rule(_constraint_m_has_prompt_upstream)


# ─── Layer 3: Build Evaluation ───────────────────────────────────────────────


@dataclass
class TestCase:
    name: str
    input: str
    expected_keywords: List[str] = field(default_factory=list)
    expected_sentiment: Optional[str] = None
    expected_schema: Optional[Dict] = None
    judge_prompt: Optional[str] = None  # free-text LLM-as-judge criteria


@dataclass
class EvaluationScore:
    accuracy: float = 0.0
    latency_p50_ms: float = 0.0
    latency_p95_ms: float = 0.0
    token_efficiency: float = 0.0
    overall: float = 0.0
    per_test: Dict[str, float] = field(default_factory=dict)
    passed: bool = False


class BuildEvaluator:
    """Run DAG against test cases and produce quality scores."""

    PUBLISH_THRESHOLD = 0.6
    EXCELLENT_THRESHOLD = 0.8

    def __init__(self, parser: DAGParser, test_cases: List[TestCase]):
        self.parser = parser
        self.test_cases = test_cases

    async def evaluate(self, agent_id: int = 0) -> EvaluationScore:
        if not self.test_cases:
            return EvaluationScore(accuracy=1.0, overall=1.0, passed=True)

        per_test: Dict[str, float] = {}
        latencies: List[float] = []

        for tc in self.test_cases:
            state = StateManager()
            runner = DAGRunner(self.parser, state_manager=state)
            t_start = time.time()
            result = await runner.run(tc.input, agent_id=agent_id)
            latencies.append(result.total_duration_ms)

            score = self._score_test_case(tc, result)
            per_test[tc.name] = score

        accuracy = sum(per_test.values()) / len(per_test) if per_test else 1.0
        latencies.sort()
        p50 = latencies[len(latencies) // 2] if latencies else 0
        p95_idx = int(len(latencies) * 0.95)
        p95 = latencies[min(p95_idx, len(latencies) - 1)] if latencies else 0

        overall = accuracy  # primary metric for now
        passed = overall >= self.PUBLISH_THRESHOLD

        return EvaluationScore(
            accuracy=accuracy,
            latency_p50_ms=p50,
            latency_p95_ms=p95,
            overall=overall,
            per_test=per_test,
            passed=passed,
        )

    def _score_test_case(self, tc: TestCase, result) -> float:
        output = str(result.final_output or "").lower()
        score = 0.0
        parts = 0

        if tc.expected_keywords:
            parts += 1
            hits = sum(1 for kw in tc.expected_keywords if kw.lower() in output)
            score += hits / len(tc.expected_keywords) if tc.expected_keywords else 1.0

        if tc.expected_sentiment:
            # Stub: would call sentiment analysis
            parts += 1
            score += 0.5  # neutral default

        if tc.expected_schema:
            parts += 1
            try:
                data = json.loads(result.final_output) if isinstance(result.final_output, str) else result.final_output
                jsonschema.validate(instance=data, schema=tc.expected_schema)
                score += 1.0
            except Exception:
                score += 0.0

        return score / parts if parts > 0 else 1.0


# ─── Layer 4: Runtime Monitoring / Degradation Detection ──────────────────────


class DegradationDetector:
    """Rolling-window quality monitor. Detects when agent performance drops below threshold."""

    WINDOW_SIZE = 100
    DEGRADATION_THRESHOLD = 0.6  # Average score below this = degraded
    WARNING_THRESHOLD = 0.8

    def __init__(self):
        self._scores: Dict[int, List[float]] = {}  # agent_id -> list of recent scores

    def record_score(self, agent_id: int, score: float):
        scores = self._scores.setdefault(agent_id, [])
        scores.append(score)
        if len(scores) > self.WINDOW_SIZE:
            scores.pop(0)

    def get_status(self, agent_id: int) -> dict:
        scores = self._scores.get(agent_id, [])
        if len(scores) < 5:  # Not enough data
            return {"status": "healthy", "avg_score": None, "sample_count": len(scores),
                    "degradation_alert": False}

        window = scores[-self.WINDOW_SIZE:]
        avg = sum(window) / len(window)
        if avg < self.DEGRADATION_THRESHOLD:
            return {"status": "degraded", "avg_score": avg, "sample_count": len(window),
                    "degradation_alert": True}
        if avg < self.WARNING_THRESHOLD:
            return {"status": "at_risk", "avg_score": avg, "sample_count": len(window),
                    "degradation_alert": True}
        return {"status": "healthy", "avg_score": avg, "sample_count": len(window),
                "degradation_alert": False}


# Global instance
degradation_detector = DegradationDetector()
