"""Pytest fixtures for backend tests.

The production engine is a module-level singleton bound to backend/data/agent_factory.db.
We patch it (and the planner_files artifacts root) BEFORE any test module imports
app code so the dev DB and dev artifacts directory are never touched.
"""

from __future__ import annotations

import sys

import pytest

# --- Interpreter / environment guard (must run before any app import) --------
# These tests require the agentscope 2.0 runtime that lives in the project venv
# (python 3.11). A bare `pytest` can resolve to ~/.local/bin/pytest whose shebang
# points at the system python3, where an older agentscope 1.x is installed:
# `agentscope.credential` is missing and `agentscope.message` lacks DataBlock.
# Without this guard that mismatch surfaces as confusing ImportErrors deep in the
# app import chain (e.g. app/core/agentscope_runner.py). Fail fast with the fix.
try:
    import agentscope.credential  # noqa: F401
    from agentscope.message import DataBlock  # noqa: F401
except ImportError as exc:  # pragma: no cover - only hit under a wrong interpreter
    pytest.exit(
        "agentscope 2.0 runtime not found in the active interpreter "
        f"({sys.executable}): {exc}.\n"
        "Run the suite with `python -m pytest` from the project venv "
        "(agentscope>=2.0), not a bare `pytest` that may resolve to "
        "~/.local/bin against the system python.",
        returncode=4,
    )

import os
import tempfile
from pathlib import Path

from sqlmodel import SQLModel, create_engine

# --- Module-level patching: must happen before any test imports app code. -----

_tmp_root = Path(tempfile.mkdtemp(prefix="agentgw-tests-"))
_db_file = _tmp_root / "test.db"
_artifacts_root = _tmp_root / "planner-sessions"

# Pin the test DB to a throwaway SQLite file BEFORE any app import. The real
# .env points DATABASE_URL at the dev PostgreSQL; tests must never touch it
# (and the PG driver may be absent). app.core.database resolves DATABASE_URL at
# import time, so the env var has to win over .env (config._load_dotenv only
# fills keys not already in os.environ). APP_ENV stays development so the
# SQLite URL is accepted rather than rejected by the production guard.
os.environ["DATABASE_URL"] = f"sqlite:///{_db_file}"
os.environ["APP_ENV"] = "development"
# Pin short-term infra to the in-process backends so tests never depend on (or
# pollute) a live Redis on the developer's machine. Tests that want Redis
# semantics inject fakeredis via the redis_client test seam. APP_ENV stays
# development; these must be set before app.core.redis_client is imported.
os.environ["ENABLE_REDIS"] = "false"
os.environ["JOB_QUEUE_PROVIDER"] = "inprocess"
# Object storage falls back to the local-disk backend in tests (no MinIO); tests
# that want MinIO semantics inject a mock via the object_storage test seam.
os.environ["ENABLE_MINIO"] = "false"
# Long-term memory writes go to PG only in tests (no mem0 dependency).
os.environ["MEMORY_PROVIDER"] = "pgonly"
os.environ["ENABLE_MEM0"] = "false"
# Pin a deterministic credential for the default `glm` provider so the
# credential-gap guard (app.core.config.credential_gap, consulted before a model
# call) doesn't fail DAG/agent execution tests on machines without a real .env.
# Tests that exercise the *missing*-credential path set their own provider env.
os.environ.setdefault("GLM_API_KEY", "test-glm-key")
os.environ.setdefault("GLM_BASE_URL", "http://test-gateway/v1")
# Pin the planner agentic flags OFF for the suite. The dev .env may enable them
# (PLANNER_AGENTIC / PLANNER_AGENTIC_ALL for manual QA); if that leaked in, every
# planner ws-integration test would route to the agentic ReAct loop, hit the real
# gateway, retry for minutes and then fall back — slow and non-deterministic.
# Tests that exercise the agentic path opt in explicitly via monkeypatch.setenv.
os.environ["PLANNER_AGENTIC"] = "off"
os.environ["PLANNER_AGENTIC_ALL"] = "off"

# Build a fresh engine bound to the temp file. Both planner_files._ROOT and
# planner.engine are reassigned so anything imported afterwards sees the
# test paths even though they captured at import time before this conftest ran.
_test_engine = create_engine(
    f"sqlite:///{_db_file}",
    echo=False,
    connect_args={"check_same_thread": False},
)

from app.core import database as _core_db  # noqa: E402
_core_db.engine = _test_engine
_core_db.DB_PATH = _db_file

from app.api import planner as _planner_api  # noqa: E402
_planner_api.engine = _test_engine

from app.api import dag as _dag_api  # noqa: E402
# dag.py goes through app.core.database.get_session rather than a module-level
# engine variable, so patching _core_db.engine above is sufficient for runtime.

from app.core import planner_files as _planner_files  # noqa: E402
_planner_files._ROOT = _artifacts_root

SQLModel.metadata.create_all(_test_engine)
