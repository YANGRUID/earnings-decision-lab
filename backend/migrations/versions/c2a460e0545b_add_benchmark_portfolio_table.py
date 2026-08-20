"""add benchmark_portfolio table

Revision ID: c2a460e0545b
Revises: 24e13c12e7dc
Create Date: 2026-08-20 21:15:47.113082

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c2a460e0545b'
down_revision: Union[str, Sequence[str], None] = '24e13c12e7dc'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('benchmark_portfolio',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=128), nullable=False),
    sa.Column('initial_capital', sa.Numeric(precision=18, scale=6), nullable=False),
    sa.Column('cash_balance', sa.Numeric(precision=18, scale=6), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_benchmark_portfolio_name'), 'benchmark_portfolio', ['name'], unique=True)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_benchmark_portfolio_name'), table_name='benchmark_portfolio')
    op.drop_table('benchmark_portfolio')
