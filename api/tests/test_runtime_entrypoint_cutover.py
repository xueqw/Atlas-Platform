import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import apps


def _agent():
    return SimpleNamespace(
        id="agent-1",
        kind="prompt",
        workspace_id="ws-1",
        created_by="user-1",
    )


def test_legacy_product_entrypoint_delegates_to_unified_runtime(monkeypatch):
    package = {"kind": "prompt", "manifest": {}, "version_no": 1, "version_label": "draft"}
    monkeypatch.setattr(apps.settings, "langgraph_runtime_enabled", True)
    monkeypatch.setattr(apps.settings, "langgraph_runtime_legacy_fallback", False)
    monkeypatch.setattr(apps, "runtime_package", lambda *_args, **_kwargs: package)
    observed = {}

    async def unified(agent, input_text, db, **kwargs):
        observed.update(kwargs)
        return {"ok": True, "answer": "unified", "run_id": "runtime-run"}

    monkeypatch.setattr(apps, "_execute_langgraph_runtime", unified)
    result = asyncio.run(apps.execute_agent_runtime(
        _agent(), "hello", object(), source="evaluate", user_id="user-1"
    ))

    assert result == {"ok": True, "answer": "unified", "run_id": "runtime-run"}
    assert observed["source"] == "evaluate"
    assert observed["package"] is package


def test_unified_runtime_never_silently_bypasses_unversioned_prompt(monkeypatch):
    package = {"kind": "prompt", "manifest": {}, "version_no": None, "version_label": "draft"}
    monkeypatch.setattr(apps.settings, "langgraph_runtime_enabled", True)
    monkeypatch.setattr(apps.settings, "langgraph_runtime_legacy_fallback", False)
    monkeypatch.setattr(apps, "runtime_package", lambda *_args, **_kwargs: package)

    async def unavailable(*_args, **_kwargs):
        return None

    monkeypatch.setattr(apps, "_execute_langgraph_runtime", unavailable)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(apps.execute_agent_runtime(_agent(), "hello", object()))
    assert exc.value.status_code == 409
