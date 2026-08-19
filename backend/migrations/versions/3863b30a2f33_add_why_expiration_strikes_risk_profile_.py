"""add why expiration strikes risk profile not alternative columns

Revision ID: 3863b30a2f33
Revises: ba7859b989cf
Create Date: 2026-08-20 00:19:22.675125

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3863b30a2f33'
down_revision: Union[str, Sequence[str], None] = 'ba7859b989cf'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Options Decision Engine V3 Part G -- deterministic explanation
    # bullets fixed at generation time, nullable (pre-existing decisions
    # never had these).
    op.add_column('ai_decision_version', sa.Column('recommended_strategy_why_expiration', sa.JSON(), nullable=True))
    op.add_column('ai_decision_version', sa.Column('recommended_strategy_why_strikes', sa.JSON(), nullable=True))
    op.add_column('ai_decision_version', sa.Column('recommended_strategy_why_risk_profile', sa.JSON(), nullable=True))
    op.add_column('ai_decision_version', sa.Column('recommended_strategy_why_not_alternative', sa.JSON(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('ai_decision_version', 'recommended_strategy_why_not_alternative')
    op.drop_column('ai_decision_version', 'recommended_strategy_why_risk_profile')
    op.drop_column('ai_decision_version', 'recommended_strategy_why_strikes')
    op.drop_column('ai_decision_version', 'recommended_strategy_why_expiration')
