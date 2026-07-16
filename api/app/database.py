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
    if engine.dialect.name == "postgresql":
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    Base.metadata.create_all(engine)
    # Governed memory is deliberately additive while legacy AgentMemory remains
    # available during the migration window.
    from .governance_models import GovernanceBase
    GovernanceBase.metadata.create_all(engine)
    if engine.dialect.name == "sqlite":
        with engine.begin() as conn:
            cols = {row[1] for row in conn.execute(text("PRAGMA table_info(document_chunks)"))}
            if "embedding" not in cols:
                conn.execute(text("ALTER TABLE document_chunks ADD COLUMN embedding TEXT"))
            dcols = {row[1] for row in conn.execute(text("PRAGMA table_info(documents)"))}
            if dcols and "object_key" not in dcols:
                conn.execute(text("ALTER TABLE documents ADD COLUMN object_key TEXT DEFAULT ''"))
            tcols = {row[1] for row in conn.execute(text("PRAGMA table_info(connector_tokens)"))}
            if tcols and "open_id" not in tcols:
                conn.execute(text("ALTER TABLE connector_tokens ADD COLUMN open_id VARCHAR(120) DEFAULT ''"))
            # 多租户迁移：给历史资源补 workspace_id 列并回填到默认工作区
            for table in ("conversations", "agents", "knowledge_bases"):
                tcols = {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))}
                if tcols and "workspace_id" not in tcols:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN workspace_id VARCHAR(36)"))
            _backfill_default_workspace(conn)
            # Agents + 版本管理迁移：老库补 kind/current_version_id/published_version_id
            acols = {row[1] for row in conn.execute(text("PRAGMA table_info(agents)"))}
            if acols and "kind" not in acols:
                conn.execute(text("ALTER TABLE agents ADD COLUMN kind VARCHAR(20) DEFAULT 'prompt'"))
            if acols and "current_version_id" not in acols:
                conn.execute(text("ALTER TABLE agents ADD COLUMN current_version_id VARCHAR(36)"))
            if acols and "published_version_id" not in acols:
                conn.execute(text("ALTER TABLE agents ADD COLUMN published_version_id VARCHAR(36)"))
            # 落地配置迁移：老库补 created_by/deploy_config_json
            if acols and "created_by" not in acols:
                conn.execute(text("ALTER TABLE agents ADD COLUMN created_by VARCHAR(36)"))
            if acols and "deploy_config_json" not in acols:
                conn.execute(text("ALTER TABLE agents ADD COLUMN deploy_config_json TEXT DEFAULT '{}'"))
            # API Key 迁移：老库 workflow_runs 补 source 列，历史记录都视为来自工作台对话
            wcols = {row[1] for row in conn.execute(text("PRAGMA table_info(workflow_runs)"))}
            if wcols and "source" not in wcols:
                conn.execute(text("ALTER TABLE workflow_runs ADD COLUMN source VARCHAR(20) DEFAULT 'chat'"))
            # Skill discovery migration: legacy rows get conservative metadata defaults.
            scols = {row[1] for row in conn.execute(text("PRAGMA table_info(skills)"))}
            skill_columns = {
                "category_path": "VARCHAR(240) DEFAULT 'general'",
                "summary": "TEXT DEFAULT ''",
                "use_when": "TEXT DEFAULT '[]'",
                "do_not_use_when": "TEXT DEFAULT '[]'",
                "examples": "TEXT DEFAULT '[]'",
                "input_schema": "TEXT DEFAULT '{}'",
                "output_schema": "TEXT DEFAULT '{}'",
                "requirements": "TEXT DEFAULT '[]'",
                "permissions": "TEXT DEFAULT '[]'",
                "version": "VARCHAR(40) DEFAULT '1.0.0'",
            }
            for name, ddl in skill_columns.items():
                if scols and name not in scols:
                    conn.execute(text(f"ALTER TABLE skills ADD COLUMN {name} {ddl}"))
            # 统一评测迁移：旧 agent_eval_runs 保留，补评测集、版本和摘要关联。
            ecols = {row[1] for row in conn.execute(text("PRAGMA table_info(agent_eval_runs)"))}
            if ecols and "suite_id" not in ecols:
                conn.execute(text("ALTER TABLE agent_eval_runs ADD COLUMN suite_id VARCHAR(36)"))
            if ecols and "agent_version_id" not in ecols:
                conn.execute(text("ALTER TABLE agent_eval_runs ADD COLUMN agent_version_id VARCHAR(36)"))
            if ecols and "summary_json" not in ecols:
                conn.execute(text("ALTER TABLE agent_eval_runs ADD COLUMN summary_json TEXT DEFAULT '{}'"))
            rcols = {row[1] for row in conn.execute(text("PRAGMA table_info(runtime_runs)"))}
            if rcols and "execution_mode" not in rcols:
                conn.execute(text("ALTER TABLE runtime_runs ADD COLUMN execution_mode VARCHAR(30) DEFAULT 'legacy'"))
    _encrypt_legacy_connector_secrets()
    # 预置测试账号 + 内置 skill（幂等，须在建表完成后；用 ORM 会话）
    from .auth import seed_test_accounts
    from .skills_seed import seed_builtin_skills
    seed_test_accounts()
    seed_builtin_skills()


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


def _encrypt_legacy_connector_secrets() -> None:
    """One-way startup migration for connector credentials written before encryption-at-rest."""
    from .encrypted_type import encrypt_text

    with engine.begin() as conn:
        rows = conn.execute(text("SELECT provider, data FROM connector_configs")).all()
        for provider, value in rows:
            if value and not value.startswith("enc:v1:"):
                conn.execute(
                    text("UPDATE connector_configs SET data = :value WHERE provider = :provider"),
                    {"provider": provider, "value": encrypt_text(value)},
                )
        rows = conn.execute(text("SELECT id, access_token, refresh_token FROM connector_tokens")).all()
        for token_id, access_token, refresh_token in rows:
            updates = {}
            if access_token and not access_token.startswith("enc:v1:"):
                updates["access_token"] = encrypt_text(access_token)
            if refresh_token and not refresh_token.startswith("enc:v1:"):
                updates["refresh_token"] = encrypt_text(refresh_token)
            if updates:
                conn.execute(
                    text("UPDATE connector_tokens SET access_token = COALESCE(:access_token, access_token), "
                         "refresh_token = COALESCE(:refresh_token, refresh_token) WHERE id = :id"),
                    {"id": token_id, "access_token": updates.get("access_token"), "refresh_token": updates.get("refresh_token")},
                )


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
