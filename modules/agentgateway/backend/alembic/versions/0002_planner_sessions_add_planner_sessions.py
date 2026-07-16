"""add planner_sessions

Revision ID: 0002_planner_sessions
Revises: 0001_baseline_schema
Create Date: 2026-05-29 16:07:44.330789

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = '0002_planner_sessions'
down_revision: Union[str, None] = '0001_baseline_schema'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('planner_sessions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('conversation_id', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('session_title', sqlmodel.sql.sqltypes.AutoString(length=200), nullable=False),
    sa.Column('stage', sqlmodel.sql.sqltypes.AutoString(length=30), nullable=False),
    sa.Column('user_request', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('requirement_summary', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('confirmed_constraints', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('task_classification', sqlmodel.sql.sqltypes.AutoString(length=50), nullable=False),
    sa.Column('latest_proposal_summary', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('planner_messages', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('file_artifacts', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('linked_agent_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('last_updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('planner_sessions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_planner_sessions_conversation_id'), ['conversation_id'], unique=True)
        batch_op.create_index(batch_op.f('ix_planner_sessions_last_updated_at'), ['last_updated_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_planner_sessions_linked_agent_id'), ['linked_agent_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('planner_sessions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_planner_sessions_linked_agent_id'))
        batch_op.drop_index(batch_op.f('ix_planner_sessions_last_updated_at'))
        batch_op.drop_index(batch_op.f('ix_planner_sessions_conversation_id'))

    op.drop_table('planner_sessions')
