"""planner run events

Revision ID: 3eb603f9d47a
Revises: f462e581c692
Create Date: 2026-06-06 20:39:23.402439

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = '3eb603f9d47a'
down_revision: Union[str, None] = 'f462e581c692'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('planner_run_events',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('run_id', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('proposal_id', sa.Integer(), nullable=True),
    sa.Column('step', sqlmodel.sql.sqltypes.AutoString(length=30), nullable=False),
    sa.Column('status', sqlmodel.sql.sqltypes.AutoString(length=20), nullable=False),
    sa.Column('message', sqlmodel.sql.sqltypes.AutoString(length=2000), nullable=False),
    sa.Column('details_json', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('progress', sa.Float(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('planner_run_events', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_planner_run_events_proposal_id'), ['proposal_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_planner_run_events_run_id'), ['run_id'], unique=False)
        batch_op.create_index('ix_planner_run_events_run_proposal', ['run_id', 'proposal_id'], unique=False)
        batch_op.create_index('ix_planner_run_events_step_status', ['step', 'status'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('planner_run_events', schema=None) as batch_op:
        batch_op.drop_index('ix_planner_run_events_step_status')
        batch_op.drop_index('ix_planner_run_events_run_proposal')
        batch_op.drop_index(batch_op.f('ix_planner_run_events_run_id'))
        batch_op.drop_index(batch_op.f('ix_planner_run_events_proposal_id'))

    op.drop_table('planner_run_events')
