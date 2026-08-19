"""add risk_profile column to ai_decision_version

Revision ID: ba7859b989cf
Revises: 796745399657
Create Date: 2026-08-19 23:59:36.155799

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ba7859b989cf'
down_revision: Union[str, Sequence[str], None] = '796745399657'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Options Decision Engine V3 Part D -- the per-decision Risk Profile
    # actually applied at generation time. Nullable: existing rows
    # predate this concept and are never backfilled with a guessed value.
    op.add_column('ai_decision_version', sa.Column('risk_profile', sa.String(length=32), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('ai_decision_version', 'risk_profile')
