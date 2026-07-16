"""Seed (idempotent) the capability_items table with type='skill' rows.

Scans ``skills/*/SKILL.md`` and ``.claude/skills/*/SKILL.md`` under the repo
root, parses each file's YAML frontmatter (``name`` / ``description`` and
optional ``metadata.hermes.related_skills``), and upserts one capability row
per skill. Run from the backend directory::

    python -m scripts.seed_planner_skills              # repo root auto-detected
    python -m scripts.seed_planner_skills --root /data/agentgateway

Re-running is safe: existing rows are updated (description / tags / config,
updated_at refreshed) while created_at is preserved; new skills are inserted.
A SKILL.md missing ``name`` or ``description`` is skipped with a stderr warning
rather than failing the whole run.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

import frontmatter


def _repo_root_default() -> Path:
    # scripts/ -> backend/ -> repo root
    return Path(__file__).resolve().parents[2]


def _iter_skill_files(root: Path):
    """Yield (skill_md_path, source) for both project and user skill dirs."""
    for base, source in ((root / "skills", "project"), (root / ".claude" / "skills", "user")):
        if not base.is_dir():
            continue
        for skill_md in sorted(base.glob("*/SKILL.md")):
            yield skill_md, source


def _related_skills(meta: dict) -> list:
    """Pull related_skills out of frontmatter.metadata.hermes if present."""
    md = meta.get("metadata")
    if isinstance(md, dict):
        hermes = md.get("hermes")
        if isinstance(hermes, dict):
            rel = hermes.get("related_skills")
            if isinstance(rel, list):
                return [str(x) for x in rel]
    rel = meta.get("related_skills")
    return [str(x) for x in rel] if isinstance(rel, list) else []


def _tags(meta: dict) -> list:
    md = meta.get("metadata")
    if isinstance(md, dict):
        hermes = md.get("hermes")
        if isinstance(hermes, dict):
            tags = hermes.get("tags")
            if isinstance(tags, list):
                return [str(x) for x in tags]
    tags = meta.get("tags")
    return [str(x) for x in tags] if isinstance(tags, list) else []


def seed(root: Path) -> tuple[int, int]:
    """Upsert all discovered skills. Returns (inserted, updated)."""
    from sqlmodel import Session, select
    from app.core.database import engine
    from app.models.db import CapabilityItem

    inserted = 0
    updated = 0

    with Session(engine) as session:
        for skill_md, source in _iter_skill_files(root):
            try:
                post = frontmatter.load(str(skill_md))
            except Exception as exc:  # noqa: BLE001
                print(f"[warn] failed to parse {skill_md}: {exc}", file=sys.stderr)
                continue
            meta = post.metadata or {}
            name = str(meta.get("name") or "").strip()
            description = str(meta.get("description") or "").strip()
            if not name or not description:
                print(
                    f"[warn] skipping {skill_md}: missing name/description in frontmatter",
                    file=sys.stderr,
                )
                continue

            rel_path = skill_md.relative_to(root).as_posix()
            config = json.dumps(
                {
                    "entrypoint": rel_path,
                    "source": source,
                    "related_skills": _related_skills(meta),
                },
                ensure_ascii=False,
            )
            tags = json.dumps(_tags(meta), ensure_ascii=False)

            row: Optional[CapabilityItem] = session.exec(
                select(CapabilityItem).where(
                    CapabilityItem.type == "skill", CapabilityItem.name == name
                )
            ).first()
            if row is None:
                row = CapabilityItem(
                    type="skill",
                    name=name,
                    description=description[:2000],
                    tags=tags,
                    config=config,
                )
                session.add(row)
                inserted += 1
            else:
                row.description = description[:2000]
                row.tags = tags
                row.config = config
                from app.models.db import _utcnow
                row.updated_at = _utcnow()
                session.add(row)
                updated += 1
        session.commit()

    return inserted, updated


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts.seed_planner_skills")
    parser.add_argument("--root", default=None, help="repo root (defaults to auto-detected)")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve() if args.root else _repo_root_default()

    inserted, updated = seed(root)
    print(f"updated {updated} skills, inserted {inserted}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
