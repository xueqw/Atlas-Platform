from app.eval_metrics import build_evaluation_summary, classify_failure, suggest_fix


# === classify_failure ===

def test_classify_failure_exit_error_takes_priority():
    assert classify_failure(False, "boom", "expected text", "logs") == "exit_error"


def test_classify_failure_output_mismatch_when_expected_missing():
    assert classify_failure(False, None, "expected text", "actual logs without it") == "output_mismatch"


def test_classify_failure_unknown_when_no_signal():
    assert classify_failure(False, None, "", "some logs") == "unknown"


# === suggest_fix ===

def test_suggest_fix_returns_specific_text_per_reason():
    assert suggest_fix("exit_error") != suggest_fix("output_mismatch")
    assert suggest_fix("output_mismatch") != suggest_fix("unknown")


def test_suggest_fix_falls_back_to_unknown_for_unrecognized_reason():
    assert suggest_fix("something_else") == suggest_fix("unknown")


# === build_evaluation_summary ===

def test_summary_full_pass_recommends_publish():
    summary = build_evaluation_summary(1.0, 120.0, [], [])
    assert summary["recommendation"] == "publish"


def test_summary_seventy_percent_recommends_optimize():
    summary = build_evaluation_summary(0.7, 120.0, [], [])
    assert summary["recommendation"] == "optimize"


def test_summary_below_seventy_percent_recommends_hold():
    summary = build_evaluation_summary(0.69, 120.0, [], [])
    assert summary["recommendation"] == "hold"


def test_summary_carries_declared_skills_and_connectors():
    summary = build_evaluation_summary(1.0, 50.0, ["sales-brief"], ["feishu"])
    assert summary["declared_skills"] == ["sales-brief"]
    assert summary["declared_connectors"] == ["feishu"]


def test_summary_rounds_avg_elapsed_ms():
    summary = build_evaluation_summary(1.0, 123.456, [], [])
    assert summary["avg_elapsed_ms"] == 123.5


# === evaluate_draft_app integration: summary + failure fields present ===

def test_evaluate_response_includes_summary_and_failure_fields(auth_client):
    draft_id = auth_client.post("/api/apps/generate", json={"message": "客服助手", "project_name": ""}).json()["draft"]["id"]

    r = auth_client.post(f"/api/apps/drafts/{draft_id}/evaluate", json={
        "cases": [
            {"name": "会通过", "input": "hi", "expected": ""},
            {"name": "会失败", "input": "hi", "expected": "这段文字绝对不会出现在输出里"},
        ],
    })
    assert r.status_code == 200, r.text
    body = r.json()

    assert "summary" in body
    assert body["summary"]["recommendation"] in ("publish", "optimize", "hold")
    assert "declared_skills" in body["summary"]
    assert "declared_connectors" in body["summary"]

    passed_case = next(item for item in body["results"] if item["name"] == "会通过")
    failed_case = next(item for item in body["results"] if item["name"] == "会失败")
    assert passed_case["failure_reason"] == ""
    assert failed_case["failure_reason"] != ""
    assert failed_case["suggestion"] != ""


def test_evaluate_full_pass_summary_recommends_publish(auth_client):
    draft_id = auth_client.post("/api/apps/generate", json={"message": "流程助手", "project_name": ""}).json()["draft"]["id"]

    r = auth_client.post(f"/api/apps/drafts/{draft_id}/evaluate", json={
        "cases": [{"name": "基础", "input": "hi", "expected": ""}],
    })
    assert r.status_code == 200, r.text
    assert r.json()["summary"]["recommendation"] == "publish"
