"""user_profiles and workspace_contexts thin tables

Revision ID: 799fa5296ca1
Revises: c9a0f147737f
Create Date: 2026-06-05 19:21:23.379987

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = '799fa5296ca1'
down_revision: Union[str, None] = 'c9a0f147737f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('user_profiles',
    sa.Column('id', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('name', sqlmodel.sql.sqltypes.AutoString(length=200), nullable=False),
    sa.Column('role', sqlmodel.sql.sqltypes.AutoString(length=100), nullable=False),
    sa.Column('department', sqlmodel.sql.sqltypes.AutoString(length=100), nullable=False),
    sa.Column('industry', sqlmodel.sql.sqltypes.AutoString(length=100), nullable=False),
    sa.Column('permissions_json', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('preferences_json', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('workspace_contexts',
    sa.Column('id', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('name', sqlmodel.sql.sqltypes.AutoString(length=200), nullable=False),
    sa.Column('project_context', sqlmodel.sql.sqltypes.AutoString(length=2000), nullable=False),
    sa.Column('connected_systems_json', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('default_entities_json', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('default_capability_tags_json', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )


def downgrade() -> None:
    op.drop_table('workspace_contexts')
    op.drop_table('user_profiles')
