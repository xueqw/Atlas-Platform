"""LangGraph checkpointer lifecycle helpers.

Checkpoint schema setup is intentionally a deployment action, not a FastAPI
startup side effect. Production callers pass the saver into a graph registry;
unit tests use LangGraph's in-memory saver directly.
"""

from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager
from typing import AsyncIterator

from .config import settings


# psycopg's async connection layer is incompatible with Windows' Proactor
# loop. This runs during application import, before Uvicorn creates its loop.
if sys.platform == "win32":  # pragma: no cover - platform-specific deployment path
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def checkpoint_database_url(url: str | None = None) -> str:
    """Return a psycopg-compatible PostgreSQL URL or reject non-durable stores."""
    resolved = (url or settings.langgraph_checkpoint_database_url or settings.database_url).strip()
    if not resolved.startswith("postgresql"):
        raise RuntimeError("LANGGRAPH_CHECKPOINT_DATABASE_URL must be a PostgreSQL URL")
    # SQLAlchemy's explicit dialect suffix is not part of psycopg's DSN syntax.
    return resolved.replace("postgresql+psycopg://", "postgresql://", 1)


@asynccontextmanager
async def open_postgres_checkpointer(url: str | None = None) -> AsyncIterator[object]:
    """Yield an async Postgres saver after deployment has initialized its schema."""
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    async with AsyncPostgresSaver.from_conn_string(checkpoint_database_url(url)) as saver:
        yield saver


async def setup_postgres_checkpoints(url: str | None = None) -> None:
    """Create/upgrade LangGraph checkpoint tables for an explicit deploy step."""
    async with open_postgres_checkpointer(url) as saver:
        await saver.setup()
