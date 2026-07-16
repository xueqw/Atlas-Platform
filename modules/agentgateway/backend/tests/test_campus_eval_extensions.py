"""Tests for campus-eval extensions: multi-turn cases, human scoring, five-dimension report."""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.api import agents as agents_api
from app.api import evaluation as eval_api
from app.core.database import engine
from app.models.db import (
    Agent, AgentTestCase, DAGGraph, EvaluationCaseResult,
    EvaluationRun, EvaluationSuite,
)


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(agents_api.router, prefix="/api")
    app.include_router(eval_api.router, prefix="/api")
    return TestClient(app)


def _create_agent(session: Session) -> int:
    agent = Agent(name="eval-test", description="x", status="testing")
    session.add(agent)
    session.commit()
    session.refresh(agent)
    return agent.id


def _create_dag(session: Session, agent_id: int) -> None:
    graph = json.dumps({
        "nodes": [
            {"id": "i1", "type": "i", "config": {}},
            {"id": "a1", "type": "agent", "config": {
                "system_prompt": "你好", "model_name": "glm-4.7-flash", "provider": "glm",
            }},
            {"id": "o1", "type": "o", "config": {}},
        ],
        "edges": [
            {"id": "e1", "source": "i1", "target": "a1"},
            {"id": "e2", "source": "a1", "target": "o1"},
        ],
    })
    session.add(DAGGraph(agent_id=agent_id, version=1, graph_json=graph, state_schema="{}"))
    session.commit()


class TestMultiTurnCaseModel:
    """AgentTestCase.turns_json field persists and is accessible."""

    def test_turns_json_persisted(self):
        with Session(engine) as s:
            agent_id = _create_agent(s)
            turns = json.dumps([
                {"user_query": "图书馆在哪？"},
                {"user_query": "几点关门？"},
                {"user_query": "周末开吗？"},
            ])
            tc = AgentTestCase(
                agent_id=agent_id, name="multi-turn-lib",
                input_message="", turns_json=turns,
                metadata_json=json.dumps({"dimension": "multi_turn"}),
            )
            s.add(tc)
            s.commit()
            s.refresh(tc)
            assert tc.turns_json == turns

            loaded = json.loads(tc.turns_json)
            assert len(loaded) == 3
            assert loaded[0]["user_query"] == "图书馆在哪？"

    def test_single_turn_case_turns_json_empty(self):
        with Session(engine) as s:
            agent_id = _create_agent(s)
            tc = AgentTestCase(
                agent_id=agent_id, name="single-turn",
                input_message="你好",
            )
            s.add(tc)
            s.commit()
            s.refresh(tc)
            assert tc.turns_json == ""


class TestMultiTurnExecution:
    """_parse_turns and _execute_case handle multi-turn correctly."""

    def test_parse_turns_empty_string(self):
        from app.core.evaluation_engine import _parse_turns
        from types import SimpleNamespace
        case = SimpleNamespace(turns_json="")
        assert _parse_turns(case) == []

    def test_parse_turns_valid_json(self):
        from app.core.evaluation_engine import _parse_turns
        from types import SimpleNamespace
        turns = [{"user_query": "hi"}, {"user_query": "bye"}]
        case = SimpleNamespace(turns_json=json.dumps(turns))
        result = _parse_turns(case)
        assert len(result) == 2
        assert result[0]["user_query"] == "hi"

    def test_parse_turns_filters_invalid_entries(self):
        from app.core.evaluation_engine import _parse_turns
        from types import SimpleNamespace
        turns = [{"user_query": "valid"}, {"no_query": True}, "not_a_dict"]
        case = SimpleNamespace(turns_json=json.dumps(turns))
        result = _parse_turns(case)
        assert len(result) == 1


class TestHumanScoreAPI:
    """PATCH human score submission."""

    def test_submit_valid_score(self, client):
        with Session(engine) as s:
            agent_id = _create_agent(s)
            suite = EvaluationSuite(agent_id=agent_id, name="hs-suite")
            s.add(suite)
            s.commit()
            s.refresh(suite)
            run = EvaluationRun(
                agent_id=agent_id, suite_id=suite.id,
                dag_version=1, summary="{}", passed=True,
            )
            s.add(run)
            s.commit()
            s.refresh(run)
            run_id = run.id
            result = EvaluationCaseResult(
                run_id=run_id, case_id=0, case_name="test",
                output="response", scores="{}", passed=True,
                duration_ms=100,
            )
            s.add(result)
            s.commit()
            s.refresh(result)
            result_id = result.id

        res = client.patch(
            f"/api/agents/{agent_id}/runs/{run_id}/results/{result_id}/score",
            json={"score": 2, "notes": "完全正确"},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["human_score"] == 2

        # Verify persisted
        with Session(engine) as s:
            r = s.get(EvaluationCaseResult, result_id)
            assert r.human_score == 2
            assert r.human_notes == "完全正确"
            assert r.human_scored_at is not None

    def test_score_out_of_range_rejected(self, client):
        with Session(engine) as s:
            agent_id = _create_agent(s)
            suite = EvaluationSuite(agent_id=agent_id, name="hs-suite-2")
            s.add(suite)
            s.commit()
            s.refresh(suite)
            run = EvaluationRun(
                agent_id=agent_id, suite_id=suite.id,
                dag_version=1, summary="{}", passed=True,
            )
            s.add(run)
            s.commit()
            s.refresh(run)
            run_id = run.id
            result = EvaluationCaseResult(
                run_id=run_id, case_id=0, case_name="test",
                output="x", scores="{}", passed=True, duration_ms=50,
            )
            s.add(result)
            s.commit()
            s.refresh(result)
            result_id = result.id

        res = client.patch(
            f"/api/agents/{agent_id}/runs/{run_id}/results/{result_id}/score",
            json={"score": 5, "notes": ""},
        )
        assert res.status_code == 422


class TestFiveDimensionReport:
    """GET report aggregation logic."""

    def _setup_run_with_results(self, session: Session):
        agent_id = _create_agent(session)
        suite = EvaluationSuite(agent_id=agent_id, name="report-suite")
        session.add(suite)
        session.commit()
        session.refresh(suite)
        run = EvaluationRun(
            agent_id=agent_id, suite_id=suite.id,
            dag_version=1, summary="{}", passed=True,
        )
        session.add(run)
        session.commit()
        session.refresh(run)

        # Create cases with different dimensions
        dims = [
            ("accuracy", 2, 100, "图书馆8:00开门"),
            ("accuracy", 1, 200, "大约8点"),
            ("coverage", None, 150, "校医院可以打HPV"),
            ("coverage", None, 120, ""),  # empty = miss
            ("multi_turn", 2, 300, "周末也开"),
            ("e2e", 2, 500, "预约成功"),
            ("e2e", 1, 400, "未完成"),
        ]
        case_ids = []
        for dim_name, h_score, dur, output in dims:
            tc = AgentTestCase(
                agent_id=agent_id, name=f"case-{dim_name}",
                input_message="q",
                metadata_json=json.dumps({"dimension": dim_name}),
            )
            session.add(tc)
            session.commit()
            session.refresh(tc)
            case_ids.append(tc.id)

            r = EvaluationCaseResult(
                run_id=run.id, case_id=tc.id, case_name=tc.name,
                output=output, scores="{}", passed=True,
                duration_ms=dur, human_score=h_score,
                human_notes="bad" if h_score == 0 else "",
            )
            session.add(r)
        session.commit()
        return agent_id, run.id

    def test_report_dimensions(self, client):
        with Session(engine) as s:
            agent_id, run_id = self._setup_run_with_results(s)

        res = client.get(f"/api/agents/{agent_id}/runs/{run_id}/report")
        assert res.status_code == 200
        report = res.json()

        assert report["run_id"] == run_id
        assert report["agent_id"] == agent_id

        dims = report["dimensions"]
        # accuracy: avg of [2, 1] = 1.5
        assert dims["accuracy"]["score"] == 1.5
        assert dims["accuracy"]["total"] == 2
        # coverage: 1 hit / 2 total = 0.5
        assert dims["coverage"]["hit_rate"] == 0.5
        assert dims["coverage"]["total"] == 2
        # multi_turn: avg of [2] = 2.0
        assert dims["multi_turn"]["score"] == 2.0
        # e2e: 1 complete (score==2) / 2 total = 0.5
        assert dims["e2e"]["completion_rate"] == 0.5
        assert dims["e2e"]["total"] == 2
        # speed: 7 cases, check p50/p95 exist
        assert dims["speed"]["total"] == 7
        assert dims["speed"]["p50_ms"] > 0

    def test_report_bad_cases(self, client):
        with Session(engine) as s:
            agent_id = _create_agent(s)
            suite = EvaluationSuite(agent_id=agent_id, name="bad-suite")
            s.add(suite)
            s.commit()
            s.refresh(suite)
            run = EvaluationRun(
                agent_id=agent_id, suite_id=suite.id,
                dag_version=1, summary="{}", passed=False,
            )
            s.add(run)
            s.commit()
            s.refresh(run)
            run_id = run.id
            tc = AgentTestCase(
                agent_id=agent_id, name="bad-case",
                input_message="x",
                metadata_json=json.dumps({"dimension": "accuracy"}),
            )
            s.add(tc)
            s.commit()
            s.refresh(tc)
            r = EvaluationCaseResult(
                run_id=run_id, case_id=tc.id, case_name="bad-case",
                output="wrong answer", scores="{}", passed=False,
                duration_ms=200, human_score=0,
                human_notes="完全错误",
            )
            s.add(r)
            s.commit()

        res = client.get(f"/api/agents/{agent_id}/runs/{run_id}/report")
        assert res.status_code == 200
        report = res.json()
        assert len(report["bad_cases"]) == 1
        assert report["bad_cases"][0]["case_name"] == "bad-case"
        assert report["bad_cases"][0]["notes"] == "完全错误"

    def test_report_no_results_409(self, client):
        with Session(engine) as s:
            agent_id = _create_agent(s)
            suite = EvaluationSuite(agent_id=agent_id, name="empty-suite")
            s.add(suite)
            s.commit()
            s.refresh(suite)
            run = EvaluationRun(
                agent_id=agent_id, suite_id=suite.id,
                dag_version=1, summary="{}", passed=False,
            )
            s.add(run)
            s.commit()
            s.refresh(run)
            run_id = run.id

        res = client.get(f"/api/agents/{agent_id}/runs/{run_id}/report")
        assert res.status_code == 409
