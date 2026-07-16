"""memory events

Revision ID: d8f6a0f975cf
Revises: 3eb603f9d47a
Create Date: 2026-06-07 19:40:35.979314

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = 'd8f6a0f975cf'
down_revision: Union[str, None] = '3eb603f9d47a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('memory_events',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('run_id', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('proposal_id', sa.Integer(), nullable=True),
    sa.Column('provider', sqlmodel.sql.sqltypes.AutoString(length=20), nullable=False),
    sa.Column('event_type', sqlmodel.sql.sqltypes.AutoString(length=20), nullable=False),
    sa.Column('scope', sqlmodel.sql.sqltypes.AutoString(length=20), nullable=False),
    sa.Column('source', sqlmodel.sql.sqltypes.AutoString(length=40), nullable=False),
    sa.Column('source_ref', sqlmodel.sql.sqltypes.AutoString(length=200), nullable=False),
    sa.Column('query_or_reason', sqlmodel.sql.sqltypes.AutoString(length=2000), nullable=False),
    sa.Column('payload_summary', sqlmodel.sql.sqltypes.AutoString(length=2000), nullable=False),
    sa.Column('payload_json', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('status', sqlmodel.sql.sqltypes.AutoString(length=20), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('memory_events', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_memory_events_proposal_id'), ['proposal_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_memory_events_run_id'), ['run_id'], unique=False)
        batch_op.create_index('ix_memory_events_run_type', ['run_id', 'event_type'], unique=False)
        batch_op.create_index('ix_memory_events_source_status', ['source', 'status'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('memory_events', schema=None) as batch_op:
        batch_op.drop_index('ix_memory_events_source_status')
        batch_op.drop_index('ix_memory_events_run_type')
        batch_op.drop_index(batch_op.f('ix_memory_events_run_id'))
        batch_op.drop_index(batch_op.f('ix_memory_events_proposal_id'))

    op.drop_table('memory_events')
