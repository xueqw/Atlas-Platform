"""add release_gate_thresholds to agents

Revision ID: 9f26e3419dd6
Revises: 278a300072fc
Create Date: 2026-05-30 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = '9f26e3419dd6'
down_revision: Union[str, None] = '278a300072fc'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('agents', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'release_gate_thresholds',
                sqlmodel.sql.sqltypes.AutoString(),
                nullable=False,
                server_default='{}',
            )
        )


def downgrade() -> None:
    with op.batch_alter_table('agents', schema=None) as batch_op:
        batch_op.drop_column('release_gate_thresholds')
