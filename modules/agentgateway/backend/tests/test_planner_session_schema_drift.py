"""Tests for fix-planner-session-schema-drift (database-migrations capability).

Covers the change's acceptance scenarios:
- 5.1 create_db_and_tables三条分支：
      alembic 不可用 → WARNING + create_all 兜底
      alembic 可用但 upgrade 失败 → ERROR + 重新抛出，不 create_all
      upgrade 成功 → 走 schema-drift 守卫
- 5.2 _assert_no_schema_drift：
      模型领先于库 → 抛 RuntimeError 且 ERROR 日志含 表名.列名
      库领先于模型 → 不报错
      列齐全 → 静默通过

All tests run against the temp SQLite engine patched in conftest. We monkeypatch
the alembic boundary (``_alembic_available`` / ``command.upgrade``) so the branch
logic is exercised without a real migration run.
"""

from __future__ import annotations

import logging

import pytest
from sqlalchemy import MetaData, Table, Column, Integer, String, inspect as sa_inspect
from sqlmodel import SQLModel

from app.core import database as db


# ─── 5.1 create_db_and_tables 三条分支 ──────────────────────────────────────

def test_alembic_unavailable_warns_and_falls_back(monkeypatch, caplog):
    """alembic 不可用 → WARNING(含 'alembic upgrade failed') + create_all 兜底，不抛。"""
    monkeypatch.setattr(db, "_alembic_available", lambda _ini: False)
    called = {"create_all": False}
    monkeypatch.setattr(
        db.SQLModel.metadata, "create_all",
        lambda engine: called.__setitem__("create_all", True),
    )

    with caplog.at_level(logging.WARNING, logger=db._log.name):
        db.create_db_and_tables()

    assert called["create_all"] is True
    assert any("alembic upgrade failed" in r.message for r in caplog.records)


def test_alembic_upgrade_failure_reraises_without_create_all(monkeypatch, caplog):
    """alembic 可用但 upgrade 失败 → ERROR + 重新抛出，绝不 create_all 掩盖。"""
    monkeypatch.setattr(db, "_alembic_available", lambda _ini: True)

    boom = RuntimeError("migration boom")

    def _raise(*_a, **_k):
        raise boom

    import alembic.command as _cmd
    monkeypatch.setattr(_cmd, "upgrade", _raise)

    create_all_called = {"v": False}
    monkeypatch.setattr(
        db.SQLModel.metadata, "create_all",
        lambda engine: create_all_called.__setitem__("v", True),
    )

    with caplog.at_level(logging.ERROR, logger=db._log.name):
        with pytest.raises(RuntimeError, match="migration boom"):
            db.create_db_and_tables()

    assert create_all_called["v"] is False, "upgrade 失败时不得退化到 create_all"
    assert any("alembic upgrade head failed" in r.message for r in caplog.records)


def test_successful_upgrade_runs_drift_guard(monkeypatch):
    """upgrade 成功 → 调用 _assert_no_schema_drift 一次。"""
    monkeypatch.setattr(db, "_alembic_available", lambda _ini: True)

    import alembic.command as _cmd
    monkeypatch.setattr(_cmd, "upgrade", lambda *_a, **_k: None)

    guard_calls = {"n": 0}
    monkeypatch.setattr(db, "_assert_no_schema_drift", lambda *_a, **_k: guard_calls.__setitem__("n", guard_calls["n"] + 1))

    db.create_db_and_tables()
    assert guard_calls["n"] == 1


# ─── 5.2 _assert_no_schema_drift ────────────────────────────────────────────

def _make_db_table(table_name: str, columns: list[str]) -> MetaData:
    """Create a real table in the test engine with exactly the given columns."""
    md = MetaData()
    Table(
        table_name, md,
        Column("id", Integer, primary_key=True),
        *[Column(c, String) for c in columns],
    )
    md.create_all(db.engine)
    return md


def _model_metadata(table_name: str, columns: list[str]) -> MetaData:
    """A standalone model-side MetaData describing the expected shape."""
    md = MetaData()
    Table(
        table_name, md,
        Column("id", Integer, primary_key=True),
        *[Column(c, String) for c in columns],
    )
    return md


def test_drift_model_ahead_raises_and_names_columns(caplog):
    """模型有、DB 没有的列 → RuntimeError 且 ERROR 日志点名 表名.列名。"""
    tbl = "drift_model_ahead"
    _make_db_table(tbl, ["a"])  # DB only has id, a
    model_md = _model_metadata(tbl, ["a", "b", "c"])  # model expects b, c too

    with caplog.at_level(logging.ERROR, logger=db._log.name):
        with pytest.raises(RuntimeError, match="schema drift"):
            db._assert_no_schema_drift(metadata=model_md)

    msg = " ".join(r.message for r in caplog.records)
    assert f"{tbl}.b" in msg
    assert f"{tbl}.c" in msg


def test_drift_db_ahead_does_not_raise(caplog):
    """DB 多出模型没有的列 → 不算 drift，静默通过。"""
    tbl = "drift_db_ahead"
    _make_db_table(tbl, ["a", "b", "extra_historical"])  # DB has an extra column
    model_md = _model_metadata(tbl, ["a", "b"])  # model only knows a, b

    with caplog.at_level(logging.ERROR, logger=db._log.name):
        db._assert_no_schema_drift(metadata=model_md)  # must not raise

    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_drift_columns_match_is_silent(caplog):
    """列齐全 → 无 ERROR 日志，静默返回。"""
    tbl = "drift_match"
    _make_db_table(tbl, ["a", "b"])
    model_md = _model_metadata(tbl, ["a", "b"])

    with caplog.at_level(logging.ERROR, logger=db._log.name):
        db._assert_no_schema_drift(metadata=model_md)

    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_drift_missing_table_is_flagged(caplog):
    """模型表在 DB 中完全不存在 → 也算 drift（点名 '<table> (table missing)'）。"""
    tbl = "drift_absent_table"
    model_md = _model_metadata(tbl, ["a"])  # never created in DB

    with caplog.at_level(logging.ERROR, logger=db._log.name):
        with pytest.raises(RuntimeError, match="schema drift"):
            db._assert_no_schema_drift(metadata=model_md)

    assert any(f"{tbl} (table missing)" in r.message for r in caplog.records)


def test_real_metadata_has_no_drift_after_create_all():
    """conftest 已对 SQLModel.metadata create_all，真实模型应无 drift。

    这同时锚定了 planner_sessions 的 4 个新列（planning_state_json 等）确实在
    模型与建表后的 DB 中一致——本次修复的核心回归点。"""
    db._assert_no_schema_drift()  # must not raise

    cols = {c["name"] for c in sa_inspect(db.engine).get_columns("planner_sessions")}
    for need in ("planning_state_json", "decision_log_json", "architecture_pattern", "apply_readiness_json"):
        assert need in cols
