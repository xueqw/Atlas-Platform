"""Regression test for the DAG observability nesting bug.

Symptom: every `dag-execution-agent-*` Langfuse trace showed only the root
span — zero node child spans, zero generations — even for multi-node DAGs that
genuinely called an LLM.

Root cause: `DAGRunner._execute_node` created node spans via
`lf.start_observation(..., parent_observation_id=None)`. The Langfuse 4.x SDK's
`start_observation` has no `parent_observation_id` parameter, so every call
raised `TypeError`, which was swallowed by a bare `except Exception: pass`. The
node spans were therefore never recorded.

Fix: nest child spans by calling `.start_observation(...)` ON the root span
object (the SDK's supported nesting API), with no `parent_observation_id` kwarg.

This test uses a fake Langfuse client that mimics the 4.x surface: its
`start_observation` REJECTS a `parent_observation_id` kwarg (as the real SDK
does), and child observations are only creatable from a span object. If the
production code regresses to the broken call, the fake raises and the node span
count drops to zero — failing the assertions below.
"""

from __future__ import annotations

import asyncio
import json
import types

from app.core.dag_executor import run_dag
from app.core import dag_executor as _dag_mod


class _FakeObservation:
    """A recorded span/generation that can spawn children from itself."""

    def __init__(self, registry, name, as_type="span", parent=None, **kw):
        self.registry = registry
        self.name = name
        self.as_type = as_type
        self.parent = parent
        self.ended = False
        self.output = None
        registry.append(self)

    # SDK 4.x: children are created FROM the parent span object. Crucially,
    # there is NO `parent_observation_id` kwarg — mirror that so a regression
    # to the old broken call raises TypeError here.
    def start_observation(self, name, as_type="span", **kw):
        return _FakeObservation(self.registry, name, as_type=as_type, parent=self, **kw)

    def update(self, **kw):
        if "output" in kw:
            self.output = kw["output"]

    def end(self):
        self.ended = True


class _FakeLangfuse:
    def __init__(self):
        self.observations: list[_FakeObservation] = []

    def create_trace_id(self):
        return "fake-trace-id"

    # Root observation. Like the real SDK, this signature has NO
    # `parent_observation_id` — passing one is a TypeError.
    def start_observation(self, trace_context=None, name=None, as_type="span", **kw):
        return _FakeObservation(self.observations, name, as_type=as_type, **kw)

    def flush(self):
        pass


def _three_node_graph() -> str:
    return json.dumps({
        "nodes": [
            {"id": "p1", "type": "p", "config": {"template": "你是助手"}},
            {"id": "a1", "type": "agent", "config": {
                "system_prompt": "简洁中文回答", "model_name": "glm-4.7-flash",
                "provider": "glm",
            }},
            {"id": "o1", "type": "o", "config": {}},
        ],
        "edges": [
            {"source": "p1", "target": "a1"},
            {"source": "a1", "target": "o1"},
        ],
    }, ensure_ascii=False)


def test_node_spans_nest_under_root(monkeypatch):
    fake = _FakeLangfuse()
    # _get_langfuse is imported into dag_executor's namespace.
    monkeypatch.setattr(_dag_mod, "_get_langfuse", lambda: fake)

    # Stub the LLM so the agent node runs without a real gateway call.
    from app.core import agentscope_runner as _runner_mod

    def _create_agent(system_prompt, model_name, provider="glm", **kwargs):
        return types.SimpleNamespace(_system_prompt=system_prompt)

    def _run_conversation(agent, user_message, attachments=None):
        async def _gen():
            yield ("token", f"echo: {user_message}")
            yield ("done", "")
        return _gen()

    monkeypatch.setattr(_runner_mod, "create_agent", _create_agent)
    monkeypatch.setattr(_runner_mod, "run_conversation", _run_conversation)

    res = asyncio.run(run_dag(_three_node_graph(), user_input="你好", agent_id=999))
    assert res.error is None

    roots = [o for o in fake.observations if o.parent is None]
    children = [o for o in fake.observations if o.parent is not None]

    # Exactly one root, named for the agent.
    assert len(roots) == 1
    assert roots[0].name == "dag-execution-agent-999"

    # Three node child spans, each parented to the root (not orphaned, not dropped).
    node_spans = [o for o in children if o.name and o.name.startswith("node-")]
    assert len(node_spans) == 3, [o.name for o in node_spans]
    assert all(o.parent is roots[0] for o in node_spans)
    assert {o.name for o in node_spans} == {
        "node-p1-p", "node-a1-agent", "node-o1-o",
    }

    # Every node span was ended (update/end path ran, no swallowed TypeError).
    assert all(o.ended for o in node_spans)
