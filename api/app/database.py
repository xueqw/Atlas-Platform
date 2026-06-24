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
    """建表 + 轻量迁移：给已存在的 document_chunks 补 embedding 列。"""
    Base.metadata.create_all(engine)
    if engine.dialect.name == "sqlite":
        with engine.begin() as conn:
            cols = {row[1] for row in conn.execute(text("PRAGMA table_info(document_chunks)"))}
            if "embedding" not in cols:
                conn.execute(text("ALTER TABLE document_chunks ADD COLUMN embedding TEXT"))
            tcols = {row[1] for row in conn.execute(text("PRAGMA table_info(connector_tokens)"))}
            if tcols and "open_id" not in tcols:
                conn.execute(text("ALTER TABLE connector_tokens ADD COLUMN open_id VARCHAR(120) DEFAULT ''"))


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
