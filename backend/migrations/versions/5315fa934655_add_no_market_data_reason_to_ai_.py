"""add no_market_data_reason to ai_decision_version

Revision ID: 5315fa934655
Revises: 0e6cd12c8226
Create Date: 2026-08-19 14:22:42.953586

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5315fa934655'
down_revision: Union[str, Sequence[str], None] = '0e6cd12c8226'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'ai_decision_version', sa.Column('no_market_data_reason', sa.Text(), nullable=True)
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('ai_decision_version', 'no_market_data_reason')
