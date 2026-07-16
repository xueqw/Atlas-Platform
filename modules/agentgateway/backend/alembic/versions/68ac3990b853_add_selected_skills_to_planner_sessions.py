"""add selected_skills to planner_sessions

Revision ID: 68ac3990b853
Revises: 0002_planner_sessions
Create Date: 2026-05-30 09:58:56.871253

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = '68ac3990b853'
down_revision: Union[str, None] = '0002_planner_sessions'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('planner_sessions', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'selected_skills',
                sqlmodel.sql.sqltypes.AutoString(),
                nullable=False,
                server_default='[]',
            )
        )


def downgrade() -> None:
    with op.batch_alter_table('planner_sessions', schema=None) as batch_op:
        batch_op.drop_column('selected_skills')
