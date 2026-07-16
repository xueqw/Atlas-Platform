"""Unit tests for app.core.model_caps.provider_for_model.

The planner LLM frequently mis-tags a model's provider in its proposal JSON,
which routes the model call to the wrong gateway and the agent silently returns
nothing. provider_for_model is the registry-authoritative correction used by
the apply pipeline; these tests pin its prefix-heuristic fallback (the path that
runs without a DB session) and the default.
"""

from __future__ import annotations

import json
import uuid

import pytest

from app.core.model_caps import provider_for_model, resolve_model_endpoint


class TestProviderPrefixHeuristic:
    """No session → pure prefix heuristic (registry lookup skipped)."""

    @pytest.mark.parametrize(
        "model_id,expected",
        [
            ("gpt-5.4", "openai"),
            ("gpt-4o", "openai"),
            ("gpt-4o-2024-08-06", "openai"),  # versioned variant
            ("GPT-4O", "openai"),             # case-insensitive
            ("o1-preview", "openai"),
            ("o3-mini", "openai"),
            ("claude-opus-4-7", "anthropic"),
            ("deepseek-v3.2", "deepseek"),
            ("qwen3.6-27b", "glm"),
            ("glm-5.1", "glm"),
            ("doubao-seed-2-0", "glm"),
            ("kimi-k2.6", "glm"),
            ("minimax-m2.5", "glm"),
        ],
    )
    def test_prefix_resolution(self, model_id, expected):
        assert provider_for_model(model_id) == expected

    def test_unknown_model_falls_back_to_default(self):
        assert provider_for_model("totally-unknown-model") == "glm"
        assert provider_for_model("totally-unknown-model", default="openai") == "openai"

    def test_empty_model_returns_default(self):
        assert provider_for_model("") == "glm"
        assert provider_for_model(None) == "glm"  # type: ignore[arg-type]

    def test_whitespace_is_trimmed(self):
        assert provider_for_model("  gpt-5.4  ") == "openai"


class TestResolveModelEndpoint:
    """resolve_model_endpoint: provider + optional capability-carried endpoint.

    Mirrors the planner's _resolve_model precedence (registry first) so DAG
    execution and the planner route the same model the same way.
    """

    def _session(self):
        from app.core.database import engine
        from sqlmodel import Session
        return Session(engine)

    def test_no_session_provider_only(self):
        # Without a session only the prefix heuristic runs — provider only.
        out = resolve_model_endpoint("gpt-4o")
        assert out == {"provider": "openai"}
        assert "base_url" not in out and "api_key" not in out

    def test_registry_model_routes_via_env_no_endpoint(self):
        from app.models.db import ModelRegistry
        mid = f"reg-model-{uuid.uuid4().hex[:8]}"
        with self._session() as s:
            s.add(ModelRegistry(
                provider="glm", model_id=mid, display_name="Reg Model",
                capability_tags="[]", context_window=8192, max_output_tokens=4096,
                input_price_per_1k=0.0, output_price_per_1k=0.0,
                supports_streaming=True, supports_vision=False, is_available=True,
            ))
            s.commit()
            out = resolve_model_endpoint(mid, session=s)
        # Registry provider wins; registry models carry no custom endpoint.
        assert out["provider"] == "glm"
        assert "base_url" not in out and "api_key" not in out

    def test_capability_model_carries_custom_endpoint(self):
        from app.models.db import CapabilityItem
        mid = f"cap-model-{uuid.uuid4().hex[:8]}"
        with self._session() as s:
            s.add(CapabilityItem(
                type="model", name="Custom Cap Model",
                config=json.dumps({
                    "model_id": mid, "provider": "openai",
                    "base_url": "https://cap.example/v1", "api_key": "valid-custom-token-not-placeholder",
                }),
                tags="[]",
            ))
            s.commit()
            out = resolve_model_endpoint(mid, session=s)
        # Not in registry → prefix heuristic gives provider; capability supplies endpoint.
        assert out["provider"] == "openai"
        assert out["base_url"] == "https://cap.example/v1"
        assert out["api_key"] == "valid-custom-token-not-placeholder"

    def test_capability_is_source_registry_ignored(self):
        # After unify-model-source-to-capability: the capability entry is the
        # single source of truth. A stale model_registry row with the same
        # model_id is IGNORED — capability provider + endpoint win.
        from app.models.db import ModelRegistry, CapabilityItem
        mid = f"dual-model-{uuid.uuid4().hex[:8]}"
        with self._session() as s:
            s.add(ModelRegistry(
                provider="glm", model_id=mid, display_name="Dual Reg",
                capability_tags="[]", context_window=8192, max_output_tokens=4096,
                input_price_per_1k=0.0, output_price_per_1k=0.0,
                supports_streaming=True, supports_vision=False, is_available=True,
            ))
            s.add(CapabilityItem(
                type="model", name="Dual Cap",
                config=json.dumps({
                    "model_id": mid, "provider": "openai",
                    "base_url": "https://cap.example/v1", "api_key": "valid-custom-token-not-placeholder",
                }),
                tags="[]",
            ))
            s.commit()
            out = resolve_model_endpoint(mid, session=s)
        # Capability wins (registry is no longer a source).
        assert out["provider"] == "openai"
        assert out["base_url"] == "https://cap.example/v1"
        assert out["api_key"] == "valid-custom-token-not-placeholder"

    def test_unknown_model_provider_only(self):
        with self._session() as s:
            out = resolve_model_endpoint(f"unknown-{uuid.uuid4().hex[:8]}", session=s)
        assert out["provider"] == "glm"
        assert "base_url" not in out and "api_key" not in out
