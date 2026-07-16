"""One-shot, idempotent migration: model_registry -> capability_items(type=model).

Part of unify-model-source-to-capability: models become single-sourced in the
capability library. This copies every model_registry row into capability_items
(type=model) so no built-in model is lost when the registry stops being a source.

Idempotent and non-destructive:
- A model_id already present in capability_items(type=model) is NOT recreated;
  only structured fields it is *missing* are backfilled, and existing values
  (especially provider / base_url / api_key) are NEVER overwritten.
- model_registry rows are left untouched (the table stays as-is; it simply
  stops being read).
- Built-in models do NOT get a stored api_key/base_url: they resolve from the
  provider's env credentials exactly as before. Only user-added capability
  models carry their own endpoint. This avoids persisting a truncated/broken key.

Run from the backend directory::

    python -m scripts.migrate_registry_to_capability --dry-run   # preview
    python -m scripts.migrate_registry_to_capability             # apply
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional

from sqlmodel import Session, select

from app.core.database import engine
from app.models.db import ModelRegistry, CapabilityItem


# Structured fields the model list (ModelRegistryResponse) reads from config.
def _config_from_registry(m: ModelRegistry) -> dict:
    return {
        "provider": m.provider,
        "model_id": m.model_id,
        "model_name": m.model_id,
        "context_window": m.context_window,
        "max_output_tokens": m.max_output_tokens,
        "input_price_per_1k": m.input_price_per_1k,
        "output_price_per_1k": m.output_price_per_1k,
        "supports_streaming": m.supports_streaming,
        "supports_vision": m.supports_vision,
        "is_available": m.is_available,
    }


def _existing_model_ids(session: Session) -> dict:
    """model_id -> CapabilityItem for every type=model capability that has one."""
    out: dict = {}
    for cap in session.exec(select(CapabilityItem).where(CapabilityItem.type == "model")).all():
        try:
            cfg = json.loads(cap.config) if isinstance(cap.config, str) else (cap.config or {})
        except (json.JSONDecodeError, TypeError):
            continue
        mid = isinstance(cfg, dict) and cfg.get("model_id")
        if mid:
            out[mid] = cap
    return out


def migrate(session: Session, dry_run: bool = False) -> dict:
    """Migrate registry rows into capability_items. Returns a summary dict.

    ``created``: model_ids newly inserted as capability items.
    ``backfilled``: model_ids that already existed and got missing fields added.
    ``unchanged``: model_ids already complete.
    """
    existing = _existing_model_ids(session)
    created: List[str] = []
    backfilled: List[str] = []
    unchanged: List[str] = []

    for m in session.exec(select(ModelRegistry)).all():
        desired = _config_from_registry(m)
        if m.model_id not in existing:
            if not dry_run:
                session.add(CapabilityItem(
                    type="model",
                    name=m.display_name or m.model_id,
                    description=f"{m.provider}/{m.model_id} — 上下文: {m.context_window}, 输出: {m.max_output_tokens}",
                    tags=m.capability_tags or "[]",
                    config=json.dumps(desired, ensure_ascii=False),
                ))
            created.append(m.model_id)
            continue

        # Backfill only MISSING structured fields; never overwrite existing
        # values (provider / base_url / api_key set by the user are preserved).
        cap = existing[m.model_id]
        try:
            cfg = json.loads(cap.config) if isinstance(cap.config, str) else (cap.config or {})
        except (json.JSONDecodeError, TypeError):
            cfg = {}
        added = False
        for k, v in desired.items():
            if k not in cfg:
                cfg[k] = v
                added = True
        if added:
            if not dry_run:
                cap.config = json.dumps(cfg, ensure_ascii=False)
                session.add(cap)
            backfilled.append(m.model_id)
        else:
            unchanged.append(m.model_id)

    if not dry_run:
        session.commit()
    return {"created": created, "backfilled": backfilled, "unchanged": unchanged}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Migrate model_registry into capability_items(type=model).")
    parser.add_argument("--dry-run", action="store_true", help="preview without writing")
    args = parser.parse_args(argv)

    with Session(engine) as session:
        summary = migrate(session, dry_run=args.dry_run)

    prefix = "[dry-run] would " if args.dry_run else ""
    print(f"{prefix}create {len(summary['created'])}: {summary['created']}")
    print(f"{prefix}backfill {len(summary['backfilled'])}: {summary['backfilled']}")
    print(f"unchanged {len(summary['unchanged'])}: {summary['unchanged']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
