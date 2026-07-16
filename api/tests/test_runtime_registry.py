import pytest

from app.runtime_registry import RuntimeGraphRegistry


def test_registry_builds_the_phase_one_graph_and_rejects_unknown_templates():
    registry = RuntimeGraphRegistry()
    graph = registry.create(RuntimeGraphRegistry.PHASE_ONE_REACT, lambda _state: "ok")
    assert graph.engine in {"langgraph", "legacy-shim"}
    with pytest.raises(ValueError, match="unknown runtime graph template"):
        registry.create("not-a-template", lambda _state: "ok")
