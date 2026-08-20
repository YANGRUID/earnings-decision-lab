"""add settlement_snapshot table

Revision ID: 24e13c12e7dc
Revises: 78ee400f83ab
Create Date: 2026-08-20 21:15:47.113082

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '24e13c12e7dc'
down_revision: Union[str, Sequence[str], None] = '78ee400f83ab'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # `capture_status` reuses the enum type created by 78ee400f83ab
    # (entry_snapshot) -- added via a separate add_column step below (see
    # 2f7efcd294db's note on why inline reuse inside create_table isn't
    # reliable).
    op.create_table('settlement_snapshot',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('decision_id', sa.Integer(), nullable=False),
    sa.Column('earnings_date', sa.Date(), nullable=False),
    sa.Column('earnings_result', sa.String(length=32), nullable=True),
    sa.Column('price_before', sa.Numeric(precision=18, scale=6), nullable=True),
    sa.Column('price_after', sa.Numeric(precision=18, scale=6), nullable=True),
    sa.Column('realized_move_pct', sa.Numeric(precision=18, scale=6), nullable=True),
    sa.Column('option_exit_value', sa.Numeric(precision=18, scale=6), nullable=True),
    sa.Column('realized_pnl', sa.Numeric(precision=18, scale=6), nullable=True),
    sa.Column('settled_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('capture_error', sa.Text(), nullable=True),
    sa.Column('source_provider', sa.String(length=64), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['decision_id'], ['decision_snapshot.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    # NOT NULL is safe directly (not nullable-then-backfill-then-alter) --
    # the table is freshly created and empty at this point in the same
    # migration.
    op.add_column(
        'settlement_snapshot',
        sa.Column(
            'status',
            sa.Enum('PENDING', 'CAPTURED', 'FAILED', 'SKIPPED', name='capture_status', create_type=False),
            nullable=False,
        ),
    )
    # Required index (decision_id).
    op.create_index(op.f('ix_settlement_snapshot_decision_id'), 'settlement_snapshot', ['decision_id'], unique=False)
    op.create_index(op.f('ix_settlement_snapshot_settled_at'), 'settlement_snapshot', ['settled_at'], unique=False)

    # "Settlement is a separate immutable event" (Phase 4.1 brief) --
    # reuses the trigger function 78ee400f83ab (entry_snapshot) already
    # created; no need to redefine it here.
    op.execute(
        """
        CREATE TRIGGER settlement_snapshot_no_update
        BEFORE UPDATE ON settlement_snapshot
        FOR EACH ROW EXECUTE FUNCTION reject_snapshot_update();
        """
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP TRIGGER IF EXISTS settlement_snapshot_no_update ON settlement_snapshot")
    op.drop_index(op.f('ix_settlement_snapshot_settled_at'), table_name='settlement_snapshot')
    op.drop_index(op.f('ix_settlement_snapshot_decision_id'), table_name='settlement_snapshot')
    op.drop_table('settlement_snapshot')
    # capture_status and reject_snapshot_update() are NOT dropped here --
    # both are owned by 78ee400f83ab (entry_snapshot), which still uses
    # them.
