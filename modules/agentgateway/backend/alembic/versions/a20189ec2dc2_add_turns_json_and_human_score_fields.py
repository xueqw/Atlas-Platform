"""add_turns_json_and_human_score_fields

Revision ID: a20189ec2dc2
Revises: d8f6a0f975cf
Create Date: 2026-06-10 09:59:59.057344

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = 'a20189ec2dc2'
down_revision: Union[str, None] = 'd8f6a0f975cf'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('agent_test_cases', schema=None) as batch_op:
        batch_op.add_column(sa.Column('turns_json', sqlmodel.sql.sqltypes.AutoString(), nullable=False, server_default=''))

    with op.batch_alter_table('evaluation_case_results', schema=None) as batch_op:
        batch_op.add_column(sa.Column('human_score', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('human_notes', sqlmodel.sql.sqltypes.AutoString(), nullable=False, server_default=''))
        batch_op.add_column(sa.Column('human_scored_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('evaluation_case_results', schema=None) as batch_op:
        batch_op.drop_column('human_scored_at')
        batch_op.drop_column('human_notes')
        batch_op.drop_column('human_score')

    with op.batch_alter_table('agent_test_cases', schema=None) as batch_op:
        batch_op.drop_column('turns_json')
