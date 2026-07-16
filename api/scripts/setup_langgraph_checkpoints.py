"""Explicit deployment command for LangGraph's PostgreSQL checkpoint tables."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Allow this deployment command to run from the repository root or api/.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.runtime_checkpoint import setup_postgres_checkpoints


if __name__ == "__main__":
    asyncio.run(setup_postgres_checkpoints())
