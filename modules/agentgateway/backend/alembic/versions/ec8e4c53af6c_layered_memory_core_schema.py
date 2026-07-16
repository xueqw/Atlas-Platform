"""layered memory core schema

Revision ID: ec8e4c53af6c
Revises: cc561d26b399
Create Date: 2026-06-01 23:38:40.923079

Adds the layered-memory core schema (Batch B):
  - three new tables: memory_items / memory_links / memory_writeback_jobs
  - Agent memory-policy columns (L3 capability layer)
  - Message retrieval-metadata columns (L1 session history)

All new columns on the existing ``agents`` / ``messages`` tables are added
NOT NULL **with a safe server_default**, so historical rows backfill the
default rather than violating the constraint (optional-first). New columns are
added via ``op.batch_alter_table`` so SQLite's limited ALTER is handled; the
same migration runs unchanged on PostgreSQL.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = 'ec8e4c53af6c'
down_revision: Union[str, None] = 'cc561d26b399'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'memory_items',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('agent_id', sa.Integer(), nullable=True),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('conversation_id', sa.Integer(), nullable=True),
        sa.Column('planner_session_id', sa.Integer(), nullable=True),
        sa.Column('memory_type', sqlmodel.sql.sqltypes.AutoString(length=30), nullable=False),
        sa.Column('scope', sqlmodel.sql.sqltypes.AutoString(length=20), nullable=False),
        sa.Column('content', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('summary', sqlmodel.sql.sqltypes.AutoString(length=2000), nullable=False),
        sa.Column('importance', sa.Float(), nullable=False),
        sa.Column('confidence', sa.Float(), nullable=False),
        sa.Column('status', sqlmodel.sql.sqltypes.AutoString(length=20), nullable=False),
        sa.Column('source_kind', sqlmodel.sql.sqltypes.AutoString(length=30), nullable=False),
        sa.Column('source_ref', sqlmodel.sql.sqltypes.AutoString(length=200), nullable=False),
        sa.Column('mem0_ref', sqlmodel.sql.sqltypes.AutoString(length=200), nullable=True),
        sa.Column('last_accessed_at', sa.DateTime(), nullable=True),
        sa.Column('access_count', sa.Integer(), nullable=False),
        sa.Column('superseded_by', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('memory_items', schema=None) as batch_op:
        batch_op.create_index('ix_memory_items_agent_status', ['agent_id', 'status'], unique=False)
        batch_op.create_index('ix_memory_items_scope_type_status', ['scope', 'memory_type', 'status'], unique=False)
        batch_op.create_index(batch_op.f('ix_memory_items_user_id'), ['user_id'], unique=False)

    op.create_table(
        'memory_links',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('from_memory_id', sa.Integer(), nullable=False),
        sa.Column('to_memory_id', sa.Integer(), nullable=False),
        sa.Column('link_type', sqlmodel.sql.sqltypes.AutoString(length=20), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('memory_links', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_memory_links_from_memory_id'), ['from_memory_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_memory_links_to_memory_id'), ['to_memory_id'], unique=False)

    op.create_table(
        'memory_writeback_jobs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('source_kind', sqlmodel.sql.sqltypes.AutoString(length=30), nullable=False),
        sa.Column('source_ref', sqlmodel.sql.sqltypes.AutoString(length=200), nullable=False),
        sa.Column('job_type', sqlmodel.sql.sqltypes.AutoString(length=30), nullable=False),
        sa.Column('status', sqlmodel.sql.sqltypes.AutoString(length=20), nullable=False),
        sa.Column('payload_json', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('error', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('finished_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('memory_writeback_jobs', schema=None) as batch_op:
        batch_op.create_index('ix_memory_writeback_jobs_status_type', ['status', 'job_type'], unique=False)

    with op.batch_alter_table('agents', schema=None) as batch_op:
        batch_op.add_column(sa.Column('memory_enabled', sa.Boolean(), nullable=False, server_default=sa.text('0')))
        batch_op.add_column(sa.Column('memory_provider', sqlmodel.sql.sqltypes.AutoString(length=20), nullable=False, server_default='none'))
        batch_op.add_column(sa.Column('memory_scope', sqlmodel.sql.sqltypes.AutoString(length=20), nullable=False, server_default=''))
        batch_op.add_column(sa.Column('memory_policy_json', sqlmodel.sql.sqltypes.AutoString(), nullable=False, server_default='{}'))
        batch_op.add_column(sa.Column('procedural_refs_json', sqlmodel.sql.sqltypes.AutoString(), nullable=False, server_default='[]'))
        batch_op.add_column(sa.Column('architecture_pattern', sqlmodel.sql.sqltypes.AutoString(length=100), nullable=False, server_default=''))

    with op.batch_alter_table('messages', schema=None) as batch_op:
        batch_op.add_column(sa.Column('message_type', sqlmodel.sql.sqltypes.AutoString(length=30), nullable=False, server_default=''))
        batch_op.add_column(sa.Column('session_kind', sqlmodel.sql.sqltypes.AutoString(length=30), nullable=False, server_default=''))
        batch_op.add_column(sa.Column('metadata_json', sqlmodel.sql.sqltypes.AutoString(), nullable=False, server_default='{}'))
        batch_op.add_column(sa.Column('importance_score', sa.Float(), nullable=False, server_default=sa.text('0')))
        batch_op.add_column(sa.Column('summary_status', sqlmodel.sql.sqltypes.AutoString(length=20), nullable=False, server_default=''))
        batch_op.add_column(sa.Column('embedding_status', sqlmodel.sql.sqltypes.AutoString(length=20), nullable=False, server_default=''))


def downgrade() -> None:
    with op.batch_alter_table('messages', schema=None) as batch_op:
        batch_op.drop_column('embedding_status')
        batch_op.drop_column('summary_status')
        batch_op.drop_column('importance_score')
        batch_op.drop_column('metadata_json')
        batch_op.drop_column('session_kind')
        batch_op.drop_column('message_type')

    with op.batch_alter_table('agents', schema=None) as batch_op:
        batch_op.drop_column('architecture_pattern')
        batch_op.drop_column('procedural_refs_json')
        batch_op.drop_column('memory_policy_json')
        batch_op.drop_column('memory_scope')
        batch_op.drop_column('memory_provider')
        batch_op.drop_column('memory_enabled')

    with op.batch_alter_table('memory_writeback_jobs', schema=None) as batch_op:
        batch_op.drop_index('ix_memory_writeback_jobs_status_type')
    op.drop_table('memory_writeback_jobs')

    with op.batch_alter_table('memory_links', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_memory_links_to_memory_id'))
        batch_op.drop_index(batch_op.f('ix_memory_links_from_memory_id'))
    op.drop_table('memory_links')

    with op.batch_alter_table('memory_items', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_memory_items_user_id'))
        batch_op.drop_index('ix_memory_items_scope_type_status')
        batch_op.drop_index('ix_memory_items_agent_status')
    op.drop_table('memory_items')
