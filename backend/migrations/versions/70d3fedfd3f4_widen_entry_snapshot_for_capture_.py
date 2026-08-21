"""widen entry_snapshot for capture attempts

Revision ID: 70d3fedfd3f4
Revises: b690e7dd35f0
Create Date: 2026-08-21 12:49:49.439168

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '70d3fedfd3f4'
down_revision: Union[str, Sequence[str], None] = 'b690e7dd35f0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # entry_snapshot has never had a real row written outside a
    # rollback-savepoint test (confirmed: no service capable of writing
    # one existed before this phase) -- safe to add capture_attempt_id/
    # leg_index directly as NOT NULL, no nullable-then-backfill dance
    # needed.
    op.add_column('entry_snapshot', sa.Column('capture_attempt_id', sa.Integer(), nullable=False))
    op.add_column('entry_snapshot', sa.Column('leg_index', sa.Integer(), nullable=False))
    op.add_column('entry_snapshot', sa.Column('external_contract_id', sa.String(length=32), nullable=True))
    op.add_column('entry_snapshot', sa.Column('quantity', sa.Integer(), nullable=True))
    op.add_column('entry_snapshot', sa.Column('multiplier', sa.Numeric(precision=18, scale=6), nullable=True))
    op.add_column('entry_snapshot', sa.Column('last_price', sa.Numeric(precision=18, scale=6), nullable=True))
    # `market_data_quality` reuses the existing enum type created for
    # options_snapshot (pre-Phase-4 schema) -- create_type=False.
    op.add_column(
        'entry_snapshot',
        sa.Column(
            'market_data_quality',
            sa.Enum('LIVE', 'DELAYED', 'FROZEN', 'UNKNOWN', name='market_data_quality', create_type=False),
            nullable=True,
        ),
    )
    op.add_column('entry_snapshot', sa.Column('pricing_source', sa.String(length=32), nullable=True))
    op.add_column('entry_snapshot', sa.Column('benchmark_entry_price', sa.Numeric(precision=18, scale=6), nullable=True))
    op.add_column('entry_snapshot', sa.Column('pricing_assumption', sa.String(length=32), nullable=True))
    op.create_index(
        op.f('ix_entry_snapshot_capture_attempt_id'), 'entry_snapshot', ['capture_attempt_id'], unique=False
    )
    op.create_unique_constraint(
        'uq_entry_snapshot_attempt_leg', 'entry_snapshot', ['capture_attempt_id', 'leg_index']
    )
    op.create_foreign_key(
        'fk_entry_snapshot_capture_attempt_id',
        'entry_snapshot', 'entry_capture_attempt', ['capture_attempt_id'], ['id'],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint('fk_entry_snapshot_capture_attempt_id', 'entry_snapshot', type_='foreignkey')
    op.drop_constraint('uq_entry_snapshot_attempt_leg', 'entry_snapshot', type_='unique')
    op.drop_index(op.f('ix_entry_snapshot_capture_attempt_id'), table_name='entry_snapshot')
    op.drop_column('entry_snapshot', 'pricing_assumption')
    op.drop_column('entry_snapshot', 'benchmark_entry_price')
    op.drop_column('entry_snapshot', 'pricing_source')
    op.drop_column('entry_snapshot', 'market_data_quality')
    op.drop_column('entry_snapshot', 'last_price')
    op.drop_column('entry_snapshot', 'multiplier')
    op.drop_column('entry_snapshot', 'quantity')
    op.drop_column('entry_snapshot', 'external_contract_id')
    op.drop_column('entry_snapshot', 'leg_index')
    op.drop_column('entry_snapshot', 'capture_attempt_id')
    # market_data_quality is NOT dropped here -- it's owned by the
    # pre-Phase-4 options_snapshot migration, which still uses it.
