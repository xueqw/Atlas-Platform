"""proposals proposal_states draft_agents

Revision ID: f462e581c692
Revises: 1bf9ebc404f4
Create Date: 2026-06-06 18:48:07.526453

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = 'f462e581c692'
down_revision: Union[str, None] = '1bf9ebc404f4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('draft_agents',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('proposal_id', sa.Integer(), nullable=False),
    sa.Column('name', sqlmodel.sql.sqltypes.AutoString(length=200), nullable=False),
    sa.Column('config_json', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('capability_refs_json', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('runtime_mode', sqlmodel.sql.sqltypes.AutoString(length=30), nullable=False),
    sa.Column('memory_policy_json', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('status', sqlmodel.sql.sqltypes.AutoString(length=20), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('draft_agents', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_draft_agents_proposal_id'), ['proposal_id'], unique=False)

    op.create_table('proposal_states',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('proposal_id', sa.Integer(), nullable=False),
    sa.Column('selected_option', sqlmodel.sql.sqltypes.AutoString(length=200), nullable=False),
    sa.Column('rejected_options_json', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('confirmed_constraints_json', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('clarification_answers_json', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('latest_summary', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('proposal_states', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_proposal_states_proposal_id'), ['proposal_id'], unique=False)

    op.create_table('proposals',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('conversation_id', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('user_goal', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('inferred_goal', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('task_type', sqlmodel.sql.sqltypes.AutoString(length=50), nullable=False),
    sa.Column('proposal_json', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('selected_expert_template_id', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=True),
    sa.Column('selected_runtime_mode', sqlmodel.sql.sqltypes.AutoString(length=30), nullable=False),
    sa.Column('status', sqlmodel.sql.sqltypes.AutoString(length=20), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('proposals', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_proposals_conversation_id'), ['conversation_id'], unique=False)
        batch_op.create_index('ix_proposals_conversation_status', ['conversation_id', 'status'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('proposals', schema=None) as batch_op:
        batch_op.drop_index('ix_proposals_conversation_status')
        batch_op.drop_index(batch_op.f('ix_proposals_conversation_id'))

    op.drop_table('proposals')
    with op.batch_alter_table('proposal_states', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_proposal_states_proposal_id'))

    op.drop_table('proposal_states')
    with op.batch_alter_table('draft_agents', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_draft_agents_proposal_id'))

    op.drop_table('draft_agents')
