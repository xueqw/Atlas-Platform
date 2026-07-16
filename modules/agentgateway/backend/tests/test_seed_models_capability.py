"""Tests: model seeding goes to capability_items, empty-only, no revival."""

from __future__ import annotations

import json
import uuid

import pytest
from sqlmodel import Session, select, delete

from app.core.database import engine
from app.models.db import CapabilityItem, ModelRegistry
from app.api import seed as seed_mod


def _model_cap_count(session: Session) -> int:
    return len([c for c in session.exec(select(CapabilityItem).where(CapabilityItem.type == "model")).all()])


class TestSeedModelsToCapability:
    def test_seeds_into_capability_when_empty(self):
        # Clean slate: no model capabilities, but registry has seed rows.
        with Session(engine) as s:
            s.exec(delete(CapabilityItem).where(CapabilityItem.type == "model"))
            s.commit()
            # ensure registry has at least one row to seed from
            if not s.exec(select(ModelRegistry)).first():
                seed_mod.seed_model_registry()
        seed_mod.seed_capabilities()
        with Session(engine) as s:
            assert _model_cap_count(s) > 0
            # seeded config carries structured fields, NOT a truncated api_key
            sample = next(c for c in s.exec(select(CapabilityItem).where(CapabilityItem.type == "model")).all())
            cfg = json.loads(sample.config)
            assert "model_id" in cfg and "provider" in cfg
            assert "..." not in str(cfg.get("api_key", ""))  # no truncated-key bug

    def test_non_empty_library_not_topped_up(self):
        # With at least one model capability present, seeding must NOT add the
        # built-in models — this is the "deleted stays deleted" guarantee.
        with Session(engine) as s:
            s.exec(delete(CapabilityItem).where(CapabilityItem.type == "model"))
            s.commit()
            s.add(CapabilityItem(
                type="model", name="Only One",
                config=json.dumps({"model_id": f"solo-{uuid.uuid4().hex[:6]}", "provider": "glm"}),
                tags="[]",
            ))
            s.commit()
            before = _model_cap_count(s)
        seed_mod.seed_capabilities()
        with Session(engine) as s:
            assert _model_cap_count(s) == before  # unchanged — no revival

    def test_deleted_builtin_stays_deleted_across_seed(self):
        # Simulate: seed populated models, user deletes one, re-seed (restart).
        with Session(engine) as s:
            s.exec(delete(CapabilityItem).where(CapabilityItem.type == "model"))
            s.commit()
        seed_mod.seed_capabilities()
        with Session(engine) as s:
            models = s.exec(select(CapabilityItem).where(CapabilityItem.type == "model")).all()
            assert models, "expected seeded models"
            victim = models[0]
            victim_name = victim.name
            s.delete(victim)
            s.commit()
        # Re-seed (as a restart would) — the deleted one must not come back.
        seed_mod.seed_capabilities()
        with Session(engine) as s:
            names = {c.name for c in s.exec(select(CapabilityItem).where(CapabilityItem.type == "model")).all()}
            assert victim_name not in names
