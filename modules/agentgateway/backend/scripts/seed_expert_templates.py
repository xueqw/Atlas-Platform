"""Idempotently seed sample expert_template cognitive assets (T3).

Inserts a couple of ``CapabilityItem(type="expert_template")`` rows the planner's
expert retrieval can match against. Run from the backend directory::

    python -m scripts.seed_expert_templates

Re-running is safe: a template already present (by type+name) is skipped. The
same function runs once at app startup (lifespan).
"""

from __future__ import annotations

from app.api.seed import seed_expert_templates


def main() -> int:
    seed_expert_templates()
    print("seeded sample expert_template capabilities")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
