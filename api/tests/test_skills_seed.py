from app.database import SessionLocal
from app.models import Skill
from sqlalchemy import select


def test_builtins_seeded_idempotent(auth_client):
    # auth_client triggered startup → seeding ran
    with SessionLocal() as db:
        builtins = db.scalars(select(Skill).where(Skill.builtin == True)).all()  # noqa: E712
    names = {b.name for b in builtins}
    assert {"销售拜访简报", "会议纪要", "中英润色"} <= names

    # re-run seeding: count must not grow
    from app.skills_seed import seed_builtin_skills
    seed_builtin_skills()
    with SessionLocal() as db:
        again = db.scalars(select(Skill).where(Skill.builtin == True)).all()
    assert len(again) == len(builtins)
