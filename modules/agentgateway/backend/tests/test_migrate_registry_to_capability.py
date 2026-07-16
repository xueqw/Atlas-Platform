"""Tests for scripts.migrate_registry_to_capability (idempotent, non-destructive)."""

from __future__ import annotations

import json
import uuid

import pytest
from sqlmodel import Session, select

from app.core.database import engine
from app.models.db import ModelRegistry, CapabilityItem
from scripts.migrate_registry_to_capability import migrate


def _reg(model_id: str, provider: str = "glm") -> ModelRegistry:
    return ModelRegistry(
        provider=provider, model_id=model_id, display_name=f"Disp {model_id}",
        capability_tags="[]", context_window=8192, max_output_tokens=4096,
        input_price_per_1k=0.0, output_price_per_1k=0.0,
        supports_streaming=True, supports_vision=False, is_available=True,
    )


def _cap_model_ids(session: Session) -> list[str]:
    out = []
    for c in session.exec(select(CapabilityItem).where(CapabilityItem.type == "model")).all():
        try:
            cfg = json.loads(c.config)
        except Exception:
            continue
        if cfg.get("model_id"):
            out.append(cfg["model_id"])
    return out


class TestMigration:
    def test_creates_missing_model(self):
        mid = f"mig-{uuid.uuid4().hex[:8]}"
        with Session(engine) as s:
            s.add(_reg(mid)); s.commit()
            summary = migrate(s, dry_run=False)
            assert mid in summary["created"]
            assert mid in _cap_model_ids(s)

    def test_idempotent_second_run_creates_nothing(self):
        mid = f"mig-{uuid.uuid4().hex[:8]}"
        with Session(engine) as s:
            s.add(_reg(mid)); s.commit()
            migrate(s, dry_run=False)
            summary2 = migrate(s, dry_run=False)
            assert mid not in summary2["created"]
            # exactly one capability for this model_id
            assert _cap_model_ids(s).count(mid) == 1

    def test_does_not_overwrite_existing_provider(self):
        mid = f"mig-{uuid.uuid4().hex[:8]}"
        with Session(engine) as s:
            # registry says glm; capability already says openai (user-set) — keep openai.
            s.add(_reg(mid, provider="glm"))
            s.add(CapabilityItem(
                type="model", name="Pre-existing",
                config=json.dumps({"model_id": mid, "provider": "openai"}), tags="[]",
            ))
            s.commit()
            migrate(s, dry_run=False)
            cap = next(c for c in s.exec(select(CapabilityItem).where(CapabilityItem.type == "model")).all()
                       if json.loads(c.config).get("model_id") == mid)
            cfg = json.loads(cap.config)
            assert cfg["provider"] == "openai"            # preserved, not overwritten
            assert cfg["context_window"] == 8192          # missing field backfilled

    def test_dry_run_writes_nothing(self):
        mid = f"mig-{uuid.uuid4().hex[:8]}"
        with Session(engine) as s:
            s.add(_reg(mid)); s.commit()
            summary = migrate(s, dry_run=True)
            assert mid in summary["created"]
            assert mid not in _cap_model_ids(s)  # not actually written
