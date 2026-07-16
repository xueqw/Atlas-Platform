"""Evaluation API — build evaluation, test case management, and monitoring."""

import json
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session, select, desc
from pydantic import BaseModel

from app.core.database import get_session
from app.core.dag_executor import DAGParser, DAGRunner, StateManager
from app.core.hermes import BuildEvaluator, TestCase, EvaluationScore
from app.core.evaluation_engine import run_suite
from app.core.evaluation_compare import compare_runs
from app.core.scorers.templates import get_template
from app.core.observability import create_trace_url
from app.models.db import (
    Agent, DAGGraph, EvaluationResult, AgentTestCase,
    EvaluationSuite, EvaluationRun, EvaluationCaseResult,
)
from app.models.schemas import (
    EvaluationRunRequest, EvaluateResponse,
    TestCaseCreate, TestCaseUpdate, TestCaseResponse,
    SuiteCreate, SuiteUpdate, SuiteResponse,
    RunResponse, RunDetailResponse, CaseResultResponse,
)

router = APIRouter(prefix="/agents", tags=["evaluation"])


# ─── Test Case CRUD ──────────────────────────────────────────────────────────


def _tc_response(tc: AgentTestCase) -> TestCaseResponse:
    return TestCaseResponse(
        id=tc.id,
        agent_id=tc.agent_id,
        suite_id=tc.suite_id,
        name=tc.name,
        input_message=tc.input_message,
        expected_keywords=tc.expected_keywords,
        expected_sentiment=tc.expected_sentiment,
        expected_schema=tc.expected_schema,
        judge_prompt=tc.judge_prompt,
        is_key=tc.is_key,
        notes=tc.notes,
        sort_order=tc.sort_order,
        created_at=tc.created_at.isoformat(),
        dimensions_json=tc.dimensions_json,
        reference_output=tc.reference_output,
        context_json=tc.context_json,
        constraints_json=tc.constraints_json,
        metadata_json=tc.metadata_json,
        turns_json=tc.turns_json,
    )


@router.get("/{agent_id}/test-cases", response_model=List[TestCaseResponse])
def list_test_cases(agent_id: int, suite_id: Optional[int] = Query(default=None),
                    session: Session = Depends(get_session)):
    agent = session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    stmt = select(AgentTestCase).where(AgentTestCase.agent_id == agent_id)
    if suite_id is not None:
        stmt = stmt.where(AgentTestCase.suite_id == suite_id)
    tcs = session.exec(stmt.order_by(AgentTestCase.sort_order)).all()
    return [_tc_response(tc) for tc in tcs]


@router.post("/{agent_id}/test-cases", response_model=TestCaseResponse)
def create_test_case(agent_id: int, payload: TestCaseCreate, session: Session = Depends(get_session)):
    agent = session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    max_order = session.exec(
        select(AgentTestCase.sort_order).where(AgentTestCase.agent_id == agent_id).order_by(AgentTestCase.sort_order.desc())
    ).first()
    tc = AgentTestCase(
        agent_id=agent_id,
        suite_id=payload.suite_id,
        name=payload.name,
        input_message=payload.input_message,
        expected_keywords=payload.expected_keywords,
        expected_sentiment=payload.expected_sentiment,
        expected_schema=payload.expected_schema,
        judge_prompt=payload.judge_prompt,
        is_key=payload.is_key,
        notes=payload.notes,
        sort_order=(max_order or 0) + 1,
        dimensions_json=payload.dimensions_json,
        reference_output=payload.reference_output,
        context_json=payload.context_json,
        constraints_json=payload.constraints_json,
        metadata_json=payload.metadata_json,
        turns_json=payload.turns_json,
    )
    session.add(tc)
    session.commit()
    session.refresh(tc)
    return _tc_response(tc)


@router.put("/{agent_id}/test-cases/{test_case_id}", response_model=TestCaseResponse)
def update_test_case(agent_id: int, test_case_id: int, payload: TestCaseUpdate, session: Session = Depends(get_session)):
    tc = session.get(AgentTestCase, test_case_id)
    if not tc or tc.agent_id != agent_id:
        raise HTTPException(status_code=404, detail="Test case not found")
    update_data = payload.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(tc, key, value)
    session.add(tc)
    session.commit()
    session.refresh(tc)
    return _tc_response(tc)


@router.delete("/{agent_id}/test-cases/{test_case_id}")
def delete_test_case(agent_id: int, test_case_id: int, session: Session = Depends(get_session)):
    tc = session.get(AgentTestCase, test_case_id)
    if not tc or tc.agent_id != agent_id:
        raise HTTPException(status_code=404, detail="Test case not found")
    session.delete(tc)
    session.commit()
    return {"ok": True}


class TestCaseItem(BaseModel):
    name: str
    input: str
    expected_keywords: List[str] = []
    expected_sentiment: Optional[str] = None
    expected_schema: Optional[dict] = None
    judge_prompt: Optional[str] = None


class EvaluateRequest(BaseModel):
    test_cases: List[TestCaseItem] = []


@router.post("/{agent_id}/evaluate", response_model=EvaluateResponse)
async def evaluate_agent(agent_id: int, payload: EvaluateRequest,
                         session: Session = Depends(get_session)):
    agent = session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    dag = session.exec(
        select(DAGGraph).where(DAGGraph.agent_id == agent_id).order_by(desc(DAGGraph.version))
    ).first()

    if not dag:
        raise HTTPException(status_code=400, detail="Agent has no DAG graph")

    parser = DAGParser(dag.graph_json)
    if parser.has_cycles():
        raise HTTPException(status_code=400, detail="DAG contains cycles")

    test_cases = [
        TestCase(
            name=tc.name,
            input=tc.input,
            expected_keywords=tc.expected_keywords,
            expected_sentiment=tc.expected_sentiment,
            expected_schema=tc.expected_schema,
            judge_prompt=tc.judge_prompt,
        )
        for tc in payload.test_cases
    ]

    evaluator = BuildEvaluator(parser, test_cases)
    score = await evaluator.evaluate(agent_id=agent_id)

    # Persist evaluation result
    test_cases_json = json.dumps([tc.model_dump() for tc in payload.test_cases])
    scores_json = json.dumps({
        "accuracy": score.accuracy,
        "latency_p50_ms": score.latency_p50_ms,
        "latency_p95_ms": score.latency_p95_ms,
        "token_efficiency": score.token_efficiency,
        "overall": score.overall,
        "per_test": score.per_test,
    })

    eval_result = EvaluationResult(
        agent_id=agent_id,
        dag_version=dag.version,
        test_cases=test_cases_json,
        scores=scores_json,
        passed=score.passed,
    )
    session.add(eval_result)
    session.commit()

    return EvaluateResponse(
        passed=score.passed,
        scores=scores_json,
        results=json.dumps(score.per_test),
    )


@router.get("/{agent_id}/evaluations", response_model=List[dict])
def list_evaluations(agent_id: int, session: Session = Depends(get_session)):
    results = session.exec(
        select(EvaluationResult).where(EvaluationResult.agent_id == agent_id)
        .order_by(desc(EvaluationResult.created_at))
    ).all()

    return [
        {
            "id": r.id,
            "agent_id": r.agent_id,
            "dag_version": r.dag_version,
            "test_cases": r.test_cases,
            "scores": r.scores,
            "passed": r.passed,
            "created_at": r.created_at.isoformat(),
        }
        for r in results
    ]


# ─── Evaluation Suites ───────────────────────────────────────────────────────


def _suite_response(suite: EvaluationSuite, case_count: int = 0) -> SuiteResponse:
    return SuiteResponse(
        id=suite.id,
        agent_id=suite.agent_id,
        name=suite.name,
        description=suite.description,
        created_at=suite.created_at.isoformat(),
        case_count=case_count,
        suite_type=suite.suite_type,
        default_dimensions_json=suite.default_dimensions_json,
        pass_threshold=suite.pass_threshold,
    )


@router.get("/{agent_id}/suites", response_model=List[SuiteResponse])
def list_suites(agent_id: int, session: Session = Depends(get_session)):
    agent = session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    suites = session.exec(
        select(EvaluationSuite).where(EvaluationSuite.agent_id == agent_id)
        .order_by(desc(EvaluationSuite.created_at))
    ).all()
    out = []
    for s in suites:
        count = len(session.exec(
            select(AgentTestCase.id).where(AgentTestCase.suite_id == s.id)
        ).all())
        out.append(_suite_response(s, count))
    return out


@router.post("/{agent_id}/suites", response_model=SuiteResponse)
def create_suite(agent_id: int, payload: SuiteCreate, session: Session = Depends(get_session)):
    agent = session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    # When the client doesn't supply dimensions, pre-fill from the type template.
    if payload.default_dimensions_json is not None:
        default_dimensions_json = payload.default_dimensions_json
    else:
        default_dimensions_json = json.dumps(
            get_template(payload.suite_type), ensure_ascii=False
        )
    suite = EvaluationSuite(
        agent_id=agent_id,
        name=payload.name,
        description=payload.description,
        suite_type=payload.suite_type,
        default_dimensions_json=default_dimensions_json,
        pass_threshold=payload.pass_threshold,
    )
    session.add(suite)
    session.commit()
    session.refresh(suite)
    return _suite_response(suite, 0)


@router.put("/{agent_id}/suites/{suite_id}", response_model=SuiteResponse)
def update_suite(agent_id: int, suite_id: int, payload: SuiteUpdate, session: Session = Depends(get_session)):
    suite = session.get(EvaluationSuite, suite_id)
    if not suite or suite.agent_id != agent_id:
        raise HTTPException(status_code=404, detail="Suite not found")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(suite, key, value)
    session.add(suite)
    session.commit()
    session.refresh(suite)
    return _suite_response(suite)


@router.delete("/{agent_id}/suites/{suite_id}")
def delete_suite(agent_id: int, suite_id: int, session: Session = Depends(get_session)):
    suite = session.get(EvaluationSuite, suite_id)
    if not suite or suite.agent_id != agent_id:
        raise HTTPException(status_code=404, detail="Suite not found")
    # Detach cases from the suite (keep the cases themselves).
    cases = session.exec(select(AgentTestCase).where(AgentTestCase.suite_id == suite_id)).all()
    for c in cases:
        c.suite_id = None
        session.add(c)
    session.delete(suite)
    session.commit()
    return {"ok": True}


# ─── Evaluation Runs ─────────────────────────────────────────────────────────


def _run_response(run: EvaluationRun) -> RunResponse:
    return RunResponse(
        id=run.id,
        agent_id=run.agent_id,
        suite_id=run.suite_id,
        dag_version=run.dag_version,
        prompt_version=run.prompt_version,
        model=run.model,
        trace_ids=run.trace_ids,
        summary=run.summary,
        passed=run.passed,
        created_at=run.created_at.isoformat(),
    )


def _case_result_response(r: EvaluationCaseResult) -> CaseResultResponse:
    try:
        dim_payload = json.loads(r.dimension_results_json or "{}")
    except json.JSONDecodeError:
        dim_payload = {}
    dimension_results = dim_payload.get("dimensions", []) if isinstance(dim_payload, dict) else []
    try:
        score_ids = json.loads(getattr(r, "langfuse_score_ids", "") or "[]")
        if not isinstance(score_ids, list):
            score_ids = []
    except json.JSONDecodeError:
        score_ids = []
    return CaseResultResponse(
        id=r.id,
        run_id=r.run_id,
        case_id=r.case_id,
        case_name=r.case_name,
        is_key=r.is_key,
        output=r.output,
        scores=r.scores,
        passed=r.passed,
        trace_id=r.trace_id,
        duration_ms=r.duration_ms,
        trace_url=create_trace_url(r.trace_id) if r.trace_id else "",
        dimension_results=dimension_results,
        dimension_results_json=r.dimension_results_json,
        evidence_json=r.evidence_json,
        langfuse_score_ids=[str(s) for s in score_ids],
        langfuse_score_count=len(score_ids),
    )


@router.post("/{agent_id}/suites/{suite_id}/runs", response_model=RunResponse)
async def create_run(agent_id: int, suite_id: int, session: Session = Depends(get_session)):
    try:
        run = await run_suite(agent_id, suite_id, session)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _run_response(run)


@router.get("/{agent_id}/runs", response_model=List[RunResponse])
def list_runs(agent_id: int, suite_id: Optional[int] = Query(default=None),
              session: Session = Depends(get_session)):
    agent = session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    stmt = select(EvaluationRun).where(EvaluationRun.agent_id == agent_id)
    if suite_id is not None:
        stmt = stmt.where(EvaluationRun.suite_id == suite_id)
    runs = session.exec(stmt.order_by(desc(EvaluationRun.created_at))).all()
    return [_run_response(r) for r in runs]


@router.get("/{agent_id}/runs/compare")
def compare_two_runs(agent_id: int, baseline_id: int = Query(...), candidate_id: int = Query(...),
                     session: Session = Depends(get_session)):
    baseline = session.get(EvaluationRun, baseline_id)
    candidate = session.get(EvaluationRun, candidate_id)
    if not baseline or baseline.agent_id != agent_id or not candidate or candidate.agent_id != agent_id:
        raise HTTPException(status_code=404, detail="Run not found")
    try:
        return compare_runs(baseline_id, candidate_id, session)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/{agent_id}/runs/{run_id}", response_model=RunDetailResponse)
def get_run(agent_id: int, run_id: int, session: Session = Depends(get_session)):
    run = session.get(EvaluationRun, run_id)
    if not run or run.agent_id != agent_id:
        raise HTTPException(status_code=404, detail="Run not found")
    case_rows = session.exec(
        select(EvaluationCaseResult).where(EvaluationCaseResult.run_id == run_id)
    ).all()
    base = _run_response(run)
    return RunDetailResponse(
        **base.model_dump(),
        case_results=[_case_result_response(r) for r in case_rows],
    )


# ─── Human Score ────────────────────────────────────────────────────────────


class HumanScorePayload(BaseModel):
    score: int  # 0=wrong, 1=partial, 2=correct
    notes: str = ""


@router.patch("/{agent_id}/runs/{run_id}/results/{result_id}/score")
def submit_human_score(agent_id: int, run_id: int, result_id: int,
                       payload: HumanScorePayload,
                       session: Session = Depends(get_session)):
    """Submit a human evaluation score for a specific case result."""
    if payload.score < 0 or payload.score > 2:
        raise HTTPException(status_code=422, detail="score must be 0, 1, or 2")
    run = session.get(EvaluationRun, run_id)
    if not run or run.agent_id != agent_id:
        raise HTTPException(status_code=404, detail="Run not found")
    result = session.get(EvaluationCaseResult, result_id)
    if not result or result.run_id != run_id:
        raise HTTPException(status_code=404, detail="Result not found")
    from datetime import datetime, timezone
    result.human_score = payload.score
    result.human_notes = payload.notes
    result.human_scored_at = datetime.now(timezone.utc).replace(tzinfo=None)
    session.add(result)
    session.commit()
    session.refresh(result)
    return {"ok": True, "result_id": result.id, "human_score": result.human_score}


# ─── Five-Dimension Report ──────────────────────────────────────────────────


@router.get("/{agent_id}/runs/{run_id}/report")
def get_run_report(agent_id: int, run_id: int, session: Session = Depends(get_session)):
    """Generate a five-dimension aggregated report for a completed evaluation run.

    Dimensions are resolved from each case's ``metadata_json.dimension`` field:
    accuracy, coverage, multi_turn, e2e, speed. Cases without a dimension tag
    are excluded from dimension-specific aggregation but still count for speed.
    """
    run = session.get(EvaluationRun, run_id)
    if not run or run.agent_id != agent_id:
        raise HTTPException(status_code=404, detail="Run not found")
    # For now, runs are synchronous so always "completed"; but guard for future async.
    suite = session.get(EvaluationSuite, run.suite_id)
    case_rows = session.exec(
        select(EvaluationCaseResult).where(EvaluationCaseResult.run_id == run_id)
    ).all()
    if not case_rows:
        raise HTTPException(status_code=409, detail="Run has no results yet")

    # Resolve dimension for each case from its AgentTestCase.metadata_json
    from app.models.db import AgentTestCase
    case_dimensions: dict = {}  # case_id → dimension string
    case_ids = [r.case_id for r in case_rows]
    if case_ids:
        cases_db = session.exec(
            select(AgentTestCase).where(AgentTestCase.id.in_(case_ids))  # type: ignore[attr-defined]
        ).all()
        for c in cases_db:
            meta = {}
            try:
                meta = json.loads(c.metadata_json or "{}")
            except (json.JSONDecodeError, TypeError):
                pass
            if isinstance(meta, dict) and meta.get("dimension"):
                case_dimensions[c.id] = str(meta["dimension"])

    # Aggregate per dimension
    accuracy_scores = []
    coverage_total = 0
    coverage_hits = 0
    multi_turn_scores = []
    e2e_total = 0
    e2e_complete = 0
    all_durations = []
    bad_cases = []

    for r in case_rows:
        dim = case_dimensions.get(r.case_id)
        all_durations.append(float(r.duration_ms))
        h_score = r.human_score

        if dim == "accuracy" and h_score is not None:
            accuracy_scores.append(h_score)
        elif dim == "coverage":
            coverage_total += 1
            if (r.output or "").strip():
                coverage_hits += 1
        elif dim == "multi_turn" and h_score is not None:
            multi_turn_scores.append(h_score)
        elif dim == "e2e":
            e2e_total += 1
            if h_score == 2:
                e2e_complete += 1

        if h_score == 0:
            bad_cases.append({
                "case_id": r.case_id,
                "case_name": r.case_name,
                "agent_response": (r.output or "")[:500],
                "notes": r.human_notes or "",
            })

    all_durations_sorted = sorted(all_durations)
    from app.core.evaluation_engine import _percentile

    report = {
        "run_id": run.id,
        "agent_id": run.agent_id,
        "suite_name": suite.name if suite else "",
        "executed_at": run.created_at.isoformat(),
        "dimensions": {
            "accuracy": {
                "score": round(sum(accuracy_scores) / len(accuracy_scores), 2) if accuracy_scores else None,
                "total": len(accuracy_scores),
                "scored": len(accuracy_scores),
            },
            "coverage": {
                "hit_rate": round(coverage_hits / coverage_total, 4) if coverage_total else None,
                "total": coverage_total,
            },
            "multi_turn": {
                "score": round(sum(multi_turn_scores) / len(multi_turn_scores), 2) if multi_turn_scores else None,
                "total": len(multi_turn_scores),
                "scored": len(multi_turn_scores),
            },
            "e2e": {
                "completion_rate": round(e2e_complete / e2e_total, 4) if e2e_total else None,
                "total": e2e_total,
            },
            "speed": {
                "p50_ms": _percentile(all_durations_sorted, 0.5),
                "p95_ms": _percentile(all_durations_sorted, 0.95),
                "total": len(all_durations),
            },
        },
        "bad_cases": bad_cases,
    }
    return report
