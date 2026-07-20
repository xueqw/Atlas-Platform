"""Tests: /models is single-sourced from capability_items(type=model)."""

from __future__ import annotations

import json
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.api import models_list as models_api
from app.core.database import engine
from app.models.db import CapabilityItem, ModelRegistry


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(models_api.router, prefix="/api")
    return TestClient(app)


def _add_cap_model(session: Session, model_id: str, provider: str = "glm", available: bool = True) -> int:
    cap = CapabilityItem(
        type="model", name=f"Disp {model_id}",
        config=json.dumps({
            "model_id": model_id, "provider": provider, "model_name": model_id,
            "context_window": 8192, "max_output_tokens": 4096,
            "supports_streaming": True, "supports_vision": False,
            "is_available": available,
        }),
        tags="[]",
    )
    session.add(cap)
    session.commit()
    session.refresh(cap)
    return cap.id


class TestListModelsSingleSource:
    def test_lists_capability_models_with_positive_id(self, client):
        mid = f"lm-{uuid.uuid4().hex[:8]}"
        with Session(engine) as s:
            cap_id = _add_cap_model(s, mid, provider="openai")
        body = client.get("/api/models").json()
        row = next((m for m in body if m["model_id"] == mid), None)
        assert row is not None
        assert row["provider"] == "openai"
        assert row["id"] == cap_id and row["id"] > 0  # positive capability id, no negative synthetic
        assert isinstance(row["configured"], bool)

    def test_unconfigured_provider_is_reported_truthfully(self, client, monkeypatch):
        mid = f"unconfigured-{uuid.uuid4().hex[:8]}"
        monkeypatch.delenv("CUSTOM_API_KEY", raising=False)
        monkeypatch.delenv("CUSTOM_BASE_URL", raising=False)
        with Session(engine) as s:
            _add_cap_model(s, mid, provider="custom")
        body = client.get("/api/models").json()
        row = next(m for m in body if m["model_id"] == mid)
        assert row["configured"] is False

    def test_configured_only_reflects_backend_credentials(self, client, monkeypatch):
        mid = f"configured-{uuid.uuid4().hex[:8]}"
        monkeypatch.delenv("AGENTGATEWAY_MODEL_ALLOWLIST", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-real-test-key-that-is-long-enough")
        monkeypatch.setenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
        with Session(engine) as s:
            _add_cap_model(s, mid, provider="openai")
        body = client.get("/api/models").json()
        row = next(m for m in body if m["model_id"] == mid)
        assert row["configured"] is True

    def test_registry_row_alone_is_not_listed(self, client):
        # A model only in model_registry (no capability) must NOT appear — the
        # registry is no longer a source.
        mid = f"regonly-{uuid.uuid4().hex[:8]}"
        with Session(engine) as s:
            s.add(ModelRegistry(
                provider="glm", model_id=mid, display_name="Reg Only",
                capability_tags="[]", context_window=8192, max_output_tokens=4096,
                input_price_per_1k=0.0, output_price_per_1k=0.0,
                supports_streaming=True, supports_vision=False, is_available=True,
            ))
            s.commit()
        body = client.get("/api/models").json()
        assert all(m["model_id"] != mid for m in body)

    def test_unavailable_model_hidden(self, client):
        mid = f"unavail-{uuid.uuid4().hex[:8]}"
        with Session(engine) as s:
            _add_cap_model(s, mid, available=False)
        body = client.get("/api/models").json()
        assert all(m["model_id"] != mid for m in body)

    def test_duplicate_model_id_deduped(self, client):
        mid = f"dup-{uuid.uuid4().hex[:8]}"
        with Session(engine) as s:
            _add_cap_model(s, mid)
            _add_cap_model(s, mid)  # second entry same model_id
        body = client.get("/api/models").json()
        assert sum(1 for m in body if m["model_id"] == mid) == 1

    def test_get_model_by_capability_id(self, client):
        mid = f"gm-{uuid.uuid4().hex[:8]}"
        with Session(engine) as s:
            cap_id = _add_cap_model(s, mid, provider="deepseek")
        res = client.get(f"/api/models/{cap_id}")
        assert res.status_code == 200
        assert res.json()["model_id"] == mid
        assert res.json()["provider"] == "deepseek"

    def test_get_model_404_for_non_model(self, client):
        # A non-model capability id must 404 from the model endpoint.
        with Session(engine) as s:
            cap = CapabilityItem(type="tool", name="t", config="{}", tags="[]")
            s.add(cap); s.commit(); s.refresh(cap)
            tool_id = cap.id
        assert client.get(f"/api/models/{tool_id}").status_code == 404
