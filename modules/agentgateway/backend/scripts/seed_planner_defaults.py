"""Idempotently seed the planner's default planning-context source rows.

Creates the single ``id="default"`` row in ``user_profiles`` and
``workspace_contexts`` that the planning_context aggregator falls back to when
no explicit user / workspace is supplied (T1: planning-context-aggregation).

Run from the backend directory::

    python -m scripts.seed_planner_defaults

Re-running is safe: an existing ``default`` row is left untouched. The same
function is called once at app startup (lifespan), so this script is mainly for
seeding an already-running / externally-migrated database.
"""

from __future__ import annotations

from app.api.seed import seed_planner_defaults


def main() -> int:
    seed_planner_defaults()
    print("seeded planner defaults (user_profiles / workspace_contexts: id=default)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
