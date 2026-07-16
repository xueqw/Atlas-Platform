import asyncio
import json

from app.evaluation import compare_run_payloads, score_execution, score_execution_with_judge
from app.models import AgentEvalRun, EvaluationCase


def test_deterministic_scorers_cover_text_keywords_schema_and_latency():
    case = EvaluationCase(
        suite_id="suite",
        name="structured",
        input_text="query",
        expected_text="done",
        scorers_json=json.dumps({
            "keywords": ["done", "A001"],
            "keyword_threshold": 1,
            "json_schema": {"required": ["status", "order_id"]},
            "max_latency_ms": 500,
        }),
    )
    execution = {"ok": True, "answer": '{"status":"done","order_id":"A001"}', "elapsed_ms": 120}
    ok, scores = score_execution(case, execution)
    assert ok is True
    assert {item["dimension"] for item in scores} == {"runtime", "contains", "keywords", "json_schema", "latency"}


def test_evaluation_run_comparison_surfaces_regressions_and_improvements():
    baseline = AgentEvalRun(
        id="baseline", agent_id="agent", kind="suite", suite_id="suite", ok=True,
        summary_json=json.dumps({"pass_rate": 1.0}),
        results_json=json.dumps([
            {"case_id": "stable", "name": "Stable", "ok": True, "elapsed_ms": 100},
            {"case_id": "improved", "name": "Improved", "ok": False, "elapsed_ms": 200},
        ]),
    )
    candidate = AgentEvalRun(
        id="candidate", agent_id="agent", kind="suite", suite_id="suite", ok=False,
        summary_json=json.dumps({"pass_rate": 0.5}),
        results_json=json.dumps([
            {"case_id": "stable", "name": "Stable", "ok": False, "elapsed_ms": 150},
            {"case_id": "improved", "name": "Improved", "ok": True, "elapsed_ms": 50},
        ]),
    )

    comparison = compare_run_payloads(baseline, candidate)

    assert comparison["deltas"]["pass_rate"] == -0.5
    assert comparison["deltas"]["average_latency_ms"] == -50.0
    assert comparison["regressions"] == [{"case_id": "stable", "name": "Stable", "is_key": False}]
    assert comparison["improvements"] == [{"case_id": "improved", "name": "Improved", "is_key": False}]


def test_evaluation_suite_crud(auth_client):
    agent = auth_client.post("/api/agents", json={"name": "Eval Agent"}).json()
    suite_response = auth_client.post(f"/api/agents/{agent['id']}/evaluation-suites", json={
        "name": "发布回归", "pass_threshold": 0.8, "is_release_gate": True,
    })
    assert suite_response.status_code == 200, suite_response.text
    suite = suite_response.json()

    case_response = auth_client.post(
        f"/api/agents/{agent['id']}/evaluation-suites/{suite['id']}/cases",
        json={
            "name": "基本回答", "input_text": "你好", "expected_text": "你好",
            "scorers": {"keywords": ["你好"]}, "is_key": True,
        },
    )
    assert case_response.status_code == 200, case_response.text

    suites = auth_client.get(f"/api/agents/{agent['id']}/evaluation-suites").json()
    assert suites[0]["name"] == "发布回归"
    assert suites[0]["cases"][0]["scorers"]["keywords"] == ["你好"]

    checklist = auth_client.get(f"/api/agents/{agent['id']}/publish-checklist").json()
    gate = next(item for item in checklist["items"] if item["key"] == "release_evaluation")
    assert gate["ok"] is False
    assert checklist["can_publish"] is False


def test_llm_judge_scores_against_rubric(monkeypatch):
    from app import evaluation as evaluation_module

    async def fake_complete(*_args, **_kwargs):
        return '```json\n{"score": 4.5, "reason": "答案准确且完整"}\n```'

    monkeypatch.setattr(evaluation_module, "complete", fake_complete)
    case = EvaluationCase(
        suite_id="suite", name="judge", input_text="解释退款规则", expected_text="",
        scorers_json=json.dumps({"judge": {"rubric": "检查准确性和完整性", "threshold": 4}}),
    )
    ok, scores = asyncio.run(score_execution_with_judge(case, {"ok": True, "answer": "退款规则说明", "elapsed_ms": 1}))
    judge = next(item for item in scores if item["dimension"] == "llm_judge")
    assert ok is True
    assert judge["score"] == 4.5
    assert judge["reason"] == "答案准确且完整"


def test_llm_judge_fails_closed_when_model_unavailable(monkeypatch):
    from app import evaluation as evaluation_module

    async def empty_complete(*_args, **_kwargs):
        return ""

    monkeypatch.setattr(evaluation_module, "complete", empty_complete)
    case = EvaluationCase(
        suite_id="suite", name="judge", input_text="q", expected_text="",
        scorers_json=json.dumps({"judge": {"rubric": "正确性", "threshold": 4}}),
    )
    ok, scores = asyncio.run(score_execution_with_judge(case, {"ok": True, "answer": "a"}))
    assert ok is False
    assert "未配置" in scores[-1]["reason"]


def test_suite_run_persists_scores_and_satisfies_release_gate(auth_client, monkeypatch):
    from app import apps as apps_module

    async def successful_runtime(*_args, **_kwargs):
        return {"ok": True, "answer": "订单 A001 已完成", "elapsed_ms": 80, "trace": [], "tool_calls": []}

    monkeypatch.setattr(apps_module, "execute_agent_runtime", successful_runtime)
    agent = auth_client.post("/api/agents", json={"name": "Runnable Eval Agent"}).json()
    suite = auth_client.post(f"/api/agents/{agent['id']}/evaluation-suites", json={
        "name": "发布回归", "pass_threshold": 1, "is_release_gate": True,
    }).json()
    auth_client.post(f"/api/agents/{agent['id']}/evaluation-suites/{suite['id']}/cases", json={
        "name": "订单状态", "input_text": "查询 A001", "expected_text": "已完成",
        "scorers": {"keywords": ["A001", "已完成"], "max_latency_ms": 500}, "is_key": True,
    })

    response = auth_client.post(f"/api/agents/{agent['id']}/evaluation-suites/{suite['id']}/run")
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["ok"] is True
    assert result["summary"]["pass_rate"] == 1
    assert {score["dimension"] for score in result["results"][0]["scores"]} == {"runtime", "contains", "keywords", "latency"}

    checklist = auth_client.get(f"/api/agents/{agent['id']}/publish-checklist").json()
    gate = next(item for item in checklist["items"] if item["key"] == "release_evaluation")
    assert gate["ok"] is True
