"""planner state enhancement

Revision ID: c9a0f147737f
Revises: ec8e4c53af6c
Create Date: 2026-06-02 00:00:00.000000

Adds richer planner state storage to ``planner_sessions`` (Batch A — Planner
Core). Four new columns hold the 14 new logical state fields (aggregated blob)
plus query-friendly projections of the decision ledger / architecture pattern /
apply-readiness:

  - planning_state_json   (default "{}")  — the 14 richer-state fields
  - decision_log_json     (default "[]")  — decisions_confirmed ledger
  - architecture_pattern  (default "")    — short pattern label, UI-filterable
  - apply_readiness_json  (default "{}")  — apply-readiness sub-structure

All columns are added NOT NULL **with a safe server_default**, so historical
rows backfill the default rather than violating the constraint (optional-first).
Columns are added via ``op.batch_alter_table`` so SQLite's limited ALTER is
handled; the same migration runs unchanged on PostgreSQL.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = 'c9a0f147737f'
down_revision: Union[str, None] = 'ec8e4c53af6c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('planner_sessions', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'planning_state_json',
                sqlmodel.sql.sqltypes.AutoString(),
                nullable=False,
                server_default='{}',
            )
        )
        batch_op.add_column(
            sa.Column(
                'decision_log_json',
                sqlmodel.sql.sqltypes.AutoString(),
                nullable=False,
                server_default='[]',
            )
        )
        batch_op.add_column(
            sa.Column(
                'architecture_pattern',
                sqlmodel.sql.sqltypes.AutoString(length=50),
                nullable=False,
                server_default='',
            )
        )
        batch_op.add_column(
            sa.Column(
                'apply_readiness_json',
                sqlmodel.sql.sqltypes.AutoString(),
                nullable=False,
                server_default='{}',
            )
        )


def downgrade() -> None:
    with op.batch_alter_table('planner_sessions', schema=None) as batch_op:
        batch_op.drop_column('apply_readiness_json')
        batch_op.drop_column('architecture_pattern')
        batch_op.drop_column('decision_log_json')
        batch_op.drop_column('planning_state_json')
