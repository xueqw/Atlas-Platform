"""add langfuse_score_ids to case results

Revision ID: 515f784fd793
Revises: 25aaba1a8dea
Create Date: 2026-05-31 11:30:00.939544

Adds the ``langfuse_score_ids`` column to evaluation_case_results (Phase 2:
Langfuse score evidence layer). NOT NULL with server_default '[]' so the ALTER
succeeds on tables that already hold rows. Autogenerate noise (the transient
_alembic_tmp table and architecture_proposals TEXT→AutoString type churn) was
removed by hand — this migration only adds the one column.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = '515f784fd793'
down_revision: Union[str, None] = '25aaba1a8dea'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('evaluation_case_results', schema=None) as batch_op:
        batch_op.add_column(sa.Column('langfuse_score_ids', sqlmodel.sql.sqltypes.AutoString(), nullable=False, server_default='[]'))


def downgrade() -> None:
    with op.batch_alter_table('evaluation_case_results', schema=None) as batch_op:
        batch_op.drop_column('langfuse_score_ids')
