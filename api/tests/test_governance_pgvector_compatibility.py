import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from app.database import _ensure_postgresql_additive_schema
from app.governance_models import SemanticFact
from app.models import Skill


def test_postgresql_models_compile_to_pgvector_and_migration_is_additive():
    assert "VECTOR(1024)" in str(CreateTable(Skill.__table__).compile(dialect=postgresql.dialect()))
    assert "VECTOR(1024)" in str(CreateTable(SemanticFact.__table__).compile(dialect=postgresql.dialect()))

    class RecordingConnection:
        def __init__(self):
            self.statements = []

        def execute(self, statement):
            self.statements.append(str(statement))

    connection = RecordingConnection()
    _ensure_postgresql_additive_schema(connection)
    sql = "\n".join(connection.statements)
    assert "ADD COLUMN IF NOT EXISTS embedding vector(1024)" in sql
    assert "DROP " not in sql.upper()


@pytest.mark.skipif(not os.getenv("ATLAS_TEST_POSTGRES_URL"), reason="set ATLAS_TEST_POSTGRES_URL for real pgvector probe")
def test_real_postgresql_pgvector_round_trip():
    engine = create_engine(os.environ["ATLAS_TEST_POSTGRES_URL"], pool_pre_ping=True)
    with engine.begin() as connection:
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        connection.execute(text("CREATE TEMP TABLE atlas_vector_probe (id integer, embedding vector(3))"))
        connection.execute(text("INSERT INTO atlas_vector_probe VALUES (1, '[1,0,0]'), (2, '[0,1,0]')"))
        nearest = connection.execute(text(
            "SELECT id FROM atlas_vector_probe ORDER BY embedding <=> CAST('[1,0,0]' AS vector) LIMIT 1"
        )).scalar_one()
    assert nearest == 1
