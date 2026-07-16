"""asset ingest runs and events

Revision ID: 1bf9ebc404f4
Revises: 799fa5296ca1
Create Date: 2026-06-06 18:16:29.046154

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = '1bf9ebc404f4'
down_revision: Union[str, None] = '799fa5296ca1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('asset_events',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('ingest_run_id', sa.Integer(), nullable=True),
    sa.Column('source_name', sqlmodel.sql.sqltypes.AutoString(length=100), nullable=False),
    sa.Column('event_type', sqlmodel.sql.sqltypes.AutoString(length=20), nullable=False),
    sa.Column('object_key', sqlmodel.sql.sqltypes.AutoString(length=400), nullable=False),
    sa.Column('source_path', sqlmodel.sql.sqltypes.AutoString(length=400), nullable=False),
    sa.Column('asset_type', sqlmodel.sql.sqltypes.AutoString(length=30), nullable=False),
    sa.Column('status', sqlmodel.sql.sqltypes.AutoString(length=20), nullable=False),
    sa.Column('message', sqlmodel.sql.sqltypes.AutoString(length=2000), nullable=False),
    sa.Column('details_json', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('asset_events', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_asset_events_ingest_run_id'), ['ingest_run_id'], unique=False)
        batch_op.create_index('ix_asset_events_run_type', ['ingest_run_id', 'event_type'], unique=False)

    op.create_table('asset_ingest_runs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('source_name', sqlmodel.sql.sqltypes.AutoString(length=100), nullable=False),
    sa.Column('source_repo', sqlmodel.sql.sqltypes.AutoString(length=300), nullable=False),
    sa.Column('storage', sqlmodel.sql.sqltypes.AutoString(length=20), nullable=False),
    sa.Column('bucket', sqlmodel.sql.sqltypes.AutoString(length=100), nullable=False),
    sa.Column('prefix', sqlmodel.sql.sqltypes.AutoString(length=300), nullable=False),
    sa.Column('manifest_key', sqlmodel.sql.sqltypes.AutoString(length=400), nullable=False),
    sa.Column('source_version', sqlmodel.sql.sqltypes.AutoString(length=100), nullable=False),
    sa.Column('status', sqlmodel.sql.sqltypes.AutoString(length=20), nullable=False),
    sa.Column('summary_json', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('completed_at', sa.DateTime(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )


def downgrade() -> None:
    op.drop_table('asset_ingest_runs')
    with op.batch_alter_table('asset_events', schema=None) as batch_op:
        batch_op.drop_index('ix_asset_events_run_type')
        batch_op.drop_index(batch_op.f('ix_asset_events_ingest_run_id'))

    op.drop_table('asset_events')
