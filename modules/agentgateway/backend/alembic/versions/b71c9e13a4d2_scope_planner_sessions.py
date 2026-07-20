"""scope planner sessions by tenant, workspace, and user

Revision ID: b71c9e13a4d2
Revises: a20189ec2dc2
Create Date: 2026-07-17 12:00:00.000000

Historical rows are assigned to the explicit ``default`` local scope. The
migration uses batch mode for SQLite and is compatible with PostgreSQL.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


revision: str = "b71c9e13a4d2"
down_revision: Union[str, None] = "a20189ec2dc2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("planner_sessions", schema=None) as batch_op:
        for name in ("tenant_id", "workspace_id", "user_id"):
            batch_op.add_column(
                sa.Column(
                    name,
                    sqlmodel.sql.sqltypes.AutoString(length=128),
                    nullable=False,
                    server_default="default",
                )
            )
            batch_op.create_index(f"ix_planner_sessions_{name}", [name], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("planner_sessions", schema=None) as batch_op:
        for name in ("user_id", "workspace_id", "tenant_id"):
            batch_op.drop_index(f"ix_planner_sessions_{name}")
            batch_op.drop_column(name)
