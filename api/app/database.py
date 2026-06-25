from pathlib import Path
from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from .config import settings


class Base(DeclarativeBase):
    pass


if settings.database_url.startswith("sqlite"):
    Path("data").mkdir(exist_ok=True)
    engine = create_engine(settings.database_url, connect_args={"check_same_thread": False})
else:
    engine = create_engine(settings.database_url, pool_pre_ping=True)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def ensure_schema():
    """建表 + 轻量迁移：补列、给历史资源回填 workspace_id、预置测试账号（多租户地基）。"""
    if engine.dialect.name == "sqlite":
        _drop_stale_auth_tables()  # 须在 create_all 之前
    Base.metadata.create_all(engine)
    if engine.dialect.name == "sqlite":
        with engine.begin() as conn:
            cols = {row[1] for row in conn.execute(text("PRAGMA table_info(document_chunks)"))}
            if "embedding" not in cols:
                conn.execute(text("ALTER TABLE document_chunks ADD COLUMN embedding TEXT"))
            tcols = {row[1] for row in conn.execute(text("PRAGMA table_info(connector_tokens)"))}
            if tcols and "open_id" not in tcols:
                conn.execute(text("ALTER TABLE connector_tokens ADD COLUMN open_id VARCHAR(120) DEFAULT ''"))
            # 多租户迁移：给历史资源补 workspace_id 列并回填到默认工作区
            for table in ("conversations", "agents", "knowledge_bases"):
                tcols = {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))}
                if tcols and "workspace_id" not in tcols:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN workspace_id VARCHAR(36)"))
            _backfill_default_workspace(conn)
    # 预置测试账号（幂等，须在建表完成后；用 ORM 会话）
    from .auth import seed_test_accounts
    seed_test_accounts()


def _drop_stale_auth_tables() -> None:
    """早期飞书 SSO 版建过 users(feishu_open_id) 表。改用账号密码后，若这些表仍是旧 schema
    且无数据，直接丢弃让 create_all 按新模型重建（空表，安全）。"""
    with engine.begin() as conn:
        ucols = {row[1] for row in conn.execute(text("PRAGMA table_info(users)"))}
        if not ucols or "username" in ucols:
            return  # 表不存在(新库) 或 已是新 schema
        count = conn.execute(text("SELECT count(*) FROM users")).scalar()
        if not count:
            for t in ("sessions", "memberships", "users"):
                conn.execute(text(f"DROP TABLE IF EXISTS {t}"))


def _backfill_default_workspace(conn) -> None:
    """若存在 workspace_id 为空的历史资源，建一个默认工作区并全部归入。"""
    import uuid
    from datetime import datetime, timezone

    orphan = any(
        conn.execute(text(f"SELECT 1 FROM {t} WHERE workspace_id IS NULL LIMIT 1")).first()
        for t in ("conversations", "agents", "knowledge_bases")
    )
    if not orphan:
        return
    row = conn.execute(text("SELECT id FROM workspaces ORDER BY created_at LIMIT 1")).first()
    if row:
        ws_id = row[0]
    else:
        ws_id = str(uuid.uuid4())
        conn.execute(
            text("INSERT INTO workspaces (id, name, created_at) VALUES (:id, :name, :ts)"),
            {"id": ws_id, "name": "默认工作区", "ts": datetime.now(timezone.utc)},
        )
    for t in ("conversations", "agents", "knowledge_bases"):
        conn.execute(text(f"UPDATE {t} SET workspace_id = :ws WHERE workspace_id IS NULL"), {"ws": ws_id})


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
