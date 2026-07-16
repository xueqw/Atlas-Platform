"""Alembic environment.

URL and target_metadata are pulled from app.core.database / app.models.db
so that database.py is the single source of truth. Importing those modules
must NOT open a DB connection or write any schema (engine is lazy, models
are class definitions only).
"""

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import engine_from_config, pool

# Make `app.*` importable when alembic runs from anywhere.
_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from app.core.database import DATABASE_URL  # noqa: E402
from app.models import db as _db_models  # noqa: F401, E402  -- ensures all SQLModel tables are registered
from sqlmodel import SQLModel  # noqa: E402

from alembic import context  # noqa: E402

# Allow operators to override the URL via env var (useful for one-off
# fresh-DB autogenerate runs without touching the dev database).
_url = os.environ.get("ALEMBIC_DATABASE_URL", DATABASE_URL)

config = context.config
config.set_main_option("sqlalchemy.url", _url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = SQLModel.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL to stdout, no connection)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        render_as_batch=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode (open a real connection)."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
