import logging
import os
from pathlib import Path

from sqlmodel import SQLModel, create_engine, Session

# Importing config has the side effect of loading backend/.env into os.environ
# (idempotent), so DATABASE_URL / APP_ENV resolve the same whether database.py
# is imported by the app, by alembic env.py, or by a test harness.
from app.core import config as _config  # noqa: F401

_log = logging.getLogger(__name__)

# Historical dev SQLite location. Kept as the dev fallback target and exported
# so tooling/tests that reference ``DB_PATH`` keep working.
DB_PATH = Path(__file__).parent.parent.parent / "data" / "agent_factory.db"
DEFAULT_SQLITE_URL = f"sqlite:///{DB_PATH}"


def _resolve_database_url() -> str:
    """Decide the active DB URL from the environment.

    - ``DATABASE_URL`` (env) wins when set; supports ``sqlite:///...`` and
      ``postgresql+psycopg://...``.
    - Unset → fall back to the local dev SQLite file.
    - ``APP_ENV=production`` MUST NOT run on SQLite (unset or sqlite URL is a
      configuration error, raised loudly rather than silently degrading).
    """
    url = (os.environ.get("DATABASE_URL") or "").strip()
    app_env = (os.environ.get("APP_ENV") or "development").strip().lower()

    if not url:
        if app_env == "production":
            raise RuntimeError(
                "DATABASE_URL is required when APP_ENV=production; refusing to "
                "fall back to SQLite as the production primary database."
            )
        return DEFAULT_SQLITE_URL

    if app_env == "production" and url.startswith("sqlite"):
        raise RuntimeError(
            "APP_ENV=production but DATABASE_URL points to SQLite; SQLite is a "
            "dev-only fallback and MUST NOT be the production primary database."
        )
    return url


def _make_engine(url: str):
    """Build an Engine with scheme-appropriate options."""
    if url.startswith("sqlite"):
        # Keep cross-thread access (FastAPI worker threads) and make sure the
        # parent dir exists for file-backed SQLite.
        if url.startswith("sqlite:///"):
            db_file = url[len("sqlite:///"):]
            if db_file and db_file != ":memory:":
                Path(db_file).parent.mkdir(parents=True, exist_ok=True)
        return create_engine(url, echo=False, connect_args={"check_same_thread": False})
    # PostgreSQL / other server DBs: pooled engine with liveness check.
    return create_engine(url, echo=False, pool_pre_ping=True)


DATABASE_URL = _resolve_database_url()
engine = _make_engine(DATABASE_URL)


def _alembic_available(alembic_ini: Path) -> bool:
    """True iff alembic can drive migrations here: the modules import AND
    ``alembic.ini`` exists. Only when this is False do we fall back to
    ``create_all`` — an *unavailable* alembic (fresh clone before install, or
    a missing ini) is the only legitimate reason to skip migrations."""
    try:
        import alembic.config  # noqa: F401
        import alembic.command  # noqa: F401
    except Exception:
        return False
    return alembic_ini.exists()


def _assert_no_schema_drift(metadata=None) -> None:
    """Fail loudly if any model table has columns the DB lacks (model leads DB).

    Runs after a successful ``alembic upgrade head``. Only the model→DB
    direction is checked: columns (or whole tables) the DB has but the model
    does not are ignored (backward compatible — historical columns are
    harmless). A drift in the other direction means a migration is missing or
    was not applied, and a ``SELECT *`` built from the model would crash at
    request time (this is exactly the planner-history 500). Surface it at
    startup instead, naming the offending ``table.column`` so the fix is
    obvious.
    """
    from sqlalchemy import inspect as sa_inspect

    metadata = metadata if metadata is not None else SQLModel.metadata
    inspector = sa_inspect(engine)
    existing_tables = set(inspector.get_table_names())

    missing: list[str] = []
    for table_name, table in metadata.tables.items():
        if table_name not in existing_tables:
            missing.append(f"{table_name} (table missing)")
            continue
        db_cols = {c["name"] for c in inspector.get_columns(table_name)}
        for col in table.columns:
            if col.name not in db_cols:
                missing.append(f"{table_name}.{col.name}")

    if missing:
        joined = ", ".join(sorted(missing))
        _log.error(
            "schema drift detected after alembic upgrade — model columns absent "
            "from the database: %s. A migration is missing or was not applied; "
            "run `python -m scripts.migrate upgrade head`.",
            joined,
        )
        raise RuntimeError(f"database schema drift: model columns missing from DB: {joined}")


def create_db_and_tables():
    """Bring the DB schema up to date.

    The managed path is ``alembic upgrade head`` so that field / table
    additions landed via migration files actually reach the running DB.

    Failure is split into two distinct cases (design D2):

    - **alembic unavailable** (modules not importable, or ``alembic.ini``
      missing — e.g. a fresh clone before ``pip install``): log a WARNING and
      fall back to ``SQLModel.metadata.create_all``. The fallback only creates
      missing *tables* — it cannot add columns to existing tables — so a real
      migration is still required for any schema change.
    - **alembic available but ``upgrade head`` fails**: log an ERROR and
      re-raise to abort startup. We do NOT fall back to ``create_all`` here:
      a partially-migrated schema silently patched by ``create_all`` is what
      lets ``model ⊋ DB`` drift hide until a request 500s.

    After a successful upgrade, ``_assert_no_schema_drift`` verifies the model
    columns all exist in the DB and aborts startup loudly if they don't (D3).
    """
    alembic_ini = Path(__file__).resolve().parents[2] / "alembic.ini"

    if not _alembic_available(alembic_ini):
        _log.warning(
            "alembic upgrade failed (alembic unavailable: modules not importable "
            "or %s missing); falling back to metadata.create_all()",
            alembic_ini,
        )
        SQLModel.metadata.create_all(engine)
        return

    from alembic.config import Config
    from alembic import command

    cfg = Config(str(alembic_ini))
    try:
        command.upgrade(cfg, "head")
    except Exception as exc:
        _log.error(
            "alembic upgrade head failed (%s: %s); refusing to fall back to "
            "create_all() — a partially-migrated schema would silently drift. "
            "Run `python -m scripts.migrate upgrade head` and fix the error.",
            type(exc).__name__,
            exc,
        )
        raise

    _assert_no_schema_drift()


def get_session():
    with Session(engine) as session:
        yield session
