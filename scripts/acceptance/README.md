# Atlas production data-plane acceptance

This suite is deliberately outside `api/tests`: the unit-test conftest forces
SQLite, while these release gates must exercise the caller-provided PostgreSQL
16, pgvector and Redis services.

Use dedicated acceptance resources. The Redis URL must select a non-zero,
disposable database because the recovery test executes `FLUSHDB`.

```bash
cd /opt/atlas
RUN_ATLAS_PRODUCTION_DATA_PLANE_TESTS=1 \
ATLAS_ACCEPTANCE_POSTGRES_URL='postgresql+psycopg://atlas:<password>@127.0.0.1:5432/atlas' \
ATLAS_ACCEPTANCE_REDIS_URL='redis://default:<password>@127.0.0.1:6379/15' \
.venv/bin/python -m pytest -q scripts/acceptance/test_production_data_plane.py
```

The suite verifies:

- PostgreSQL major version 16 and the real pgvector extension;
- 1024-dimensional Skill and Semantic Memory vector writes, tenant filtering,
  database-side ordering/threshold/limit, and valid/transaction-time behavior;
- LangGraph checkpoint recovery through a newly opened PostgreSQL saver;
- concurrent Runtime idempotency, row-locked event sequencing, and cursor replay;
- Redis TTL behavior and reconstruction of Session Context plus Runtime state
  from PostgreSQL after complete loss of the dedicated Redis database.

The opt-in flag does not make a shared Redis database safe. Never point this
suite at database 0 or at a database used by another environment.
