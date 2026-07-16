"""Audit (and optionally fix) provider conflicts between the two model sources.

A model id can appear both in ``model_registry`` and as a capability-library
item (``capability_items`` type=model, config.model_id). The model list and
provider resolution treat the registry as authoritative, so a capability row
that declares a *different* provider for the same model id is a latent
inconsistency: it can mislead anyone reading the capability library and, before
the registry-first fixes, routed execution to the wrong gateway.

This is a READ-ONLY audit by default — it never changes the DB unless ``--fix``
is passed, and even then only rewrites the capability row's provider to match
the registry (registry is never modified). Run from the backend directory::

    python -m scripts.audit_model_providers          # report only
    python -m scripts.audit_model_providers --fix     # converge capability → registry

No schema change, no migration.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional

from sqlmodel import Session, select

from app.core.database import engine
from app.models.db import ModelRegistry, CapabilityItem


def find_provider_conflicts(session: Session) -> List[dict]:
    """Return one entry per model id whose capability provider != registry provider.

    Each entry: ``{model_id, registry_provider, capability_provider, capability_item_id}``.
    Read-only.
    """
    reg_by_id = {
        m.model_id: m.provider
        for m in session.exec(select(ModelRegistry)).all()
        if m.model_id
    }
    conflicts: List[dict] = []
    for cap in session.exec(select(CapabilityItem).where(CapabilityItem.type == "model")).all():
        try:
            cfg = json.loads(cap.config) if isinstance(cap.config, str) else (cap.config or {})
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(cfg, dict):
            continue
        mid = cfg.get("model_id")
        cap_provider = cfg.get("provider")
        if not mid or mid not in reg_by_id:
            continue
        if cap_provider and cap_provider != reg_by_id[mid]:
            conflicts.append({
                "model_id": mid,
                "registry_provider": reg_by_id[mid],
                "capability_provider": cap_provider,
                "capability_item_id": cap.id,
            })
    return conflicts


def converge_to_registry(session: Session, conflicts: Optional[List[dict]] = None) -> int:
    """Rewrite each conflicting capability row's provider to the registry value.

    Registry is never touched. Returns the number of capability rows updated.
    Only call from an explicit --fix run (or a confirmed maintenance action).
    """
    conflicts = conflicts if conflicts is not None else find_provider_conflicts(session)
    updated = 0
    for c in conflicts:
        cap = session.get(CapabilityItem, c["capability_item_id"])
        if cap is None:
            continue
        try:
            cfg = json.loads(cap.config) if isinstance(cap.config, str) else (cap.config or {})
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(cfg, dict):
            continue
        cfg["provider"] = c["registry_provider"]
        cap.config = json.dumps(cfg, ensure_ascii=False)
        session.add(cap)
        updated += 1
    if updated:
        session.commit()
    return updated


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Audit model provider conflicts (registry vs capability).")
    parser.add_argument("--fix", action="store_true",
                        help="rewrite conflicting capability providers to match the registry")
    args = parser.parse_args(argv)

    with Session(engine) as session:
        conflicts = find_provider_conflicts(session)
        if not conflicts:
            print("no provider conflicts: every shared model_id agrees across registry and capability.")
            return 0
        print(f"found {len(conflicts)} provider conflict(s) (registry is authoritative):")
        for c in conflicts:
            print(f"  - {c['model_id']}: registry={c['registry_provider']} "
                  f"capability={c['capability_provider']} (cap item id={c['capability_item_id']})")
        if args.fix:
            n = converge_to_registry(session, conflicts)
            print(f"fixed: rewrote {n} capability row(s) to the registry provider.")
        else:
            print("run with --fix to converge capability providers to the registry.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
