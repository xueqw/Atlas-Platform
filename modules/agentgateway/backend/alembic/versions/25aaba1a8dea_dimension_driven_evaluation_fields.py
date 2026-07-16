"""dimension-driven evaluation fields

Revision ID: 25aaba1a8dea
Revises: a1b2c3d4e5f6
Create Date: 2026-05-31 10:30:28.643853

Adds dimension-driven evaluation columns to three tables. Every column is
NOT NULL with a server_default aligned to the SQLModel default, so the ALTER
succeeds on tables that already hold rows.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = '25aaba1a8dea'
down_revision: Union[str, None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('agent_test_cases', schema=None) as batch_op:
        batch_op.add_column(sa.Column('dimensions_json', sqlmodel.sql.sqltypes.AutoString(), nullable=False, server_default=''))
        batch_op.add_column(sa.Column('reference_output', sqlmodel.sql.sqltypes.AutoString(), nullable=False, server_default=''))
        batch_op.add_column(sa.Column('context_json', sqlmodel.sql.sqltypes.AutoString(), nullable=False, server_default='{}'))
        batch_op.add_column(sa.Column('constraints_json', sqlmodel.sql.sqltypes.AutoString(), nullable=False, server_default='{}'))
        batch_op.add_column(sa.Column('metadata_json', sqlmodel.sql.sqltypes.AutoString(), nullable=False, server_default='{}'))

    with op.batch_alter_table('evaluation_case_results', schema=None) as batch_op:
        batch_op.add_column(sa.Column('dimension_results_json', sqlmodel.sql.sqltypes.AutoString(), nullable=False, server_default='{}'))
        batch_op.add_column(sa.Column('evidence_json', sqlmodel.sql.sqltypes.AutoString(), nullable=False, server_default='{}'))

    with op.batch_alter_table('evaluation_suites', schema=None) as batch_op:
        batch_op.add_column(sa.Column('suite_type', sqlmodel.sql.sqltypes.AutoString(length=50), nullable=False, server_default='general'))
        batch_op.add_column(sa.Column('default_dimensions_json', sqlmodel.sql.sqltypes.AutoString(), nullable=False, server_default='{}'))
        batch_op.add_column(sa.Column('pass_threshold', sa.Float(), nullable=False, server_default='0.6'))


def downgrade() -> None:
    with op.batch_alter_table('evaluation_suites', schema=None) as batch_op:
        batch_op.drop_column('pass_threshold')
        batch_op.drop_column('default_dimensions_json')
        batch_op.drop_column('suite_type')

    with op.batch_alter_table('evaluation_case_results', schema=None) as batch_op:
        batch_op.drop_column('evidence_json')
        batch_op.drop_column('dimension_results_json')

    with op.batch_alter_table('agent_test_cases', schema=None) as batch_op:
        batch_op.drop_column('metadata_json')
        batch_op.drop_column('constraints_json')
        batch_op.drop_column('context_json')
        batch_op.drop_column('reference_output')
        batch_op.drop_column('dimensions_json')
