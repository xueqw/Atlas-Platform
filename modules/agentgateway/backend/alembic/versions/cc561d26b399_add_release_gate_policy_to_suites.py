"""add release_gate_policy to suites

Revision ID: cc561d26b399
Revises: 515f784fd793
Create Date: 2026-06-01 11:50:08.722794

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = 'cc561d26b399'
down_revision: Union[str, None] = '515f784fd793'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add EvaluationSuite.release_gate_policy_json (Phase 3 release-gate policy
    # override). server_default='{}' so the column backfills on existing rows
    # without a NOT NULL violation; SQLite needs batch_alter_table for ADD COLUMN
    # with a default. (Autogenerate noise on unrelated tables stripped.)
    with op.batch_alter_table('evaluation_suites', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'release_gate_policy_json',
            sqlmodel.sql.sqltypes.AutoString(),
            nullable=False,
            server_default='{}',
        ))


def downgrade() -> None:
    with op.batch_alter_table('evaluation_suites', schema=None) as batch_op:
        batch_op.drop_column('release_gate_policy_json')
