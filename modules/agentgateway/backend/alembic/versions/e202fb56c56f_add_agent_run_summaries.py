"""add agent_run_summaries

Revision ID: e202fb56c56f
Revises: 68ac3990b853
Create Date: 2026-05-30 18:57:21.834891

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = 'e202fb56c56f'
down_revision: Union[str, None] = '68ac3990b853'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('agent_run_summaries',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('agent_id', sa.Integer(), nullable=False),
    sa.Column('dag_version', sa.Integer(), nullable=False),
    sa.Column('trace_id', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('status', sqlmodel.sql.sqltypes.AutoString(length=20), nullable=False),
    sa.Column('total_duration_ms', sa.Integer(), nullable=False),
    sa.Column('token_input', sa.Integer(), nullable=False),
    sa.Column('token_output', sa.Integer(), nullable=False),
    sa.Column('node_count', sa.Integer(), nullable=False),
    sa.Column('error', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('agent_run_summaries', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_agent_run_summaries_agent_id'), ['agent_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_agent_run_summaries_created_at'), ['created_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_agent_run_summaries_trace_id'), ['trace_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('agent_run_summaries', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_agent_run_summaries_trace_id'))
        batch_op.drop_index(batch_op.f('ix_agent_run_summaries_created_at'))
        batch_op.drop_index(batch_op.f('ix_agent_run_summaries_agent_id'))

    op.drop_table('agent_run_summaries')
