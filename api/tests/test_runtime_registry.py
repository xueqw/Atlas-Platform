import pytest

from app.runtime_registry import RuntimeGraphRegistry


def test_registry_builds_the_phase_one_graph_and_rejects_unknown_templates():
    registry = RuntimeGraphRegistry()
    graph = registry.create(RuntimeGraphRegistry.PHASE_ONE_REACT, lambda _state: "ok")
    assert graph.engine in {"langgraph", "legacy-shim"}
    with pytest.raises(ValueError, match="unknown runtime graph template"):
        registry.create("not-a-template", lambda _state: "ok")


def test_registry_exposes_all_versioned_execution_templates():
    registry = RuntimeGraphRegistry()

    def unused(*_args):
        raise AssertionError("construction must not execute callbacks")

    react = registry.create(RuntimeGraphRegistry.REACT_V1, lambda _state: "ok")
    sequential = registry.create(
        RuntimeGraphRegistry.PLAN_EXECUTE_REVIEW_V1,
        None,
        planner=unused,
        executor=unused,
        reviewer=unused,
    )
    multi = registry.create(
        RuntimeGraphRegistry.MULTI_AGENT_PLAN_EXECUTE_REVIEW_V1,
        None,
        planner=unused,
        executor=unused,
        reviewer=unused,
    )
    assert react.engine in {"langgraph", "legacy-shim"}
    assert sequential.engine in {"langgraph", "legacy-shim"}
    assert multi.engine in {"langgraph", "legacy-shim"}
    assert sequential.strategy.value == "plan-execute-review"
    assert multi.strategy.value == "multi-agent-plan-execute-review"
