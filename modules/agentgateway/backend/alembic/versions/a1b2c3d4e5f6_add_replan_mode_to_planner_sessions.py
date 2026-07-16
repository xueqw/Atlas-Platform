"""add replan mode + context to planner_sessions

Revision ID: a1b2c3d4e5f6
Revises: 9f26e3419dd6
Create Date: 2026-05-30 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = '9f26e3419dd6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('planner_sessions', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'mode',
                sqlmodel.sql.sqltypes.AutoString(),
                nullable=False,
                server_default='create',
            )
        )
        batch_op.add_column(
            sa.Column(
                'replan_context',
                sqlmodel.sql.sqltypes.AutoString(),
                nullable=False,
                server_default='',
            )
        )


def downgrade() -> None:
    with op.batch_alter_table('planner_sessions', schema=None) as batch_op:
        batch_op.drop_column('replan_context')
        batch_op.drop_column('mode')
