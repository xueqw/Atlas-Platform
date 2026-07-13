from __future__ import annotations

import argparse
from datetime import datetime
import os

from sqlalchemy import Boolean, DateTime, MetaData, create_engine, insert, select, text


def convert(value, column):
    if value is None:
        return None
    if isinstance(column.type, DateTime) and isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    if isinstance(column.type, Boolean):
        return bool(value)
    return value


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("sqlite_path")
    parser.add_argument("--target", default=os.environ.get("DATABASE_URL", ""))
    args = parser.parse_args()
    if not args.target.startswith("postgresql"):
        raise SystemExit("--target must be a PostgreSQL SQLAlchemy URL")

    source = create_engine(f"sqlite:///{os.path.abspath(args.sqlite_path)}")
    target = create_engine(args.target, pool_pre_ping=True)
    with target.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))

    from app.database import Base  # config has already read DATABASE_URL
    import app.models  # noqa: F401
    Base.metadata.create_all(target)
    source_meta = MetaData()
    source_meta.reflect(source)

    copied = 0
    with source.connect() as src, target.begin() as dst:
        for table in Base.metadata.sorted_tables:
            old = source_meta.tables.get(table.name)
            if old is None:
                continue
            common = [column for column in table.columns if column.name in old.c]
            rows = src.execute(select(*[old.c[column.name] for column in common])).mappings()
            payload = [{column.name: convert(row[column.name], column) for column in common} for row in rows]
            if payload:
                dst.execute(insert(table), payload)
                copied += len(payload)
                print(f"{table.name}: {len(payload)}")
    print(f"migration complete: {copied} rows")


if __name__ == "__main__":
    main()
