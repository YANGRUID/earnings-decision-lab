"""add entry_snapshot table

Revision ID: 78ee400f83ab
Revises: 2f7efcd294db
Create Date: 2026-08-20 21:15:47.113082

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '78ee400f83ab'
down_revision: Union[str, Sequence[str], None] = '2f7efcd294db'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # `option_type` reuses the existing enum type created by
    # c535e22e3a6d (options_snapshot) -- added via a separate add_column
    # step below (inline reuse inside create_table does not reliably
    # honor create_type=False; see 2f7efcd294db's note). `capture_status`
    # and `option_action` are new here, safe to create inline.
    op.create_table('entry_snapshot',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('decision_id', sa.Integer(), nullable=False),
    sa.Column(
        'status',
        sa.Enum('PENDING', 'CAPTURED', 'FAILED', 'SKIPPED', name='capture_status'),
        nullable=False,
    ),
    sa.Column('captured_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('expiration', sa.Date(), nullable=True),
    sa.Column('strike', sa.Numeric(precision=18, scale=6), nullable=True),
    sa.Column('action', sa.Enum('BUY', 'SELL', name='option_action'), nullable=True),
    sa.Column('bid', sa.Numeric(precision=18, scale=6), nullable=True),
    sa.Column('ask', sa.Numeric(precision=18, scale=6), nullable=True),
    sa.Column('mid', sa.Numeric(precision=18, scale=6), nullable=True),
    sa.Column('implied_volatility', sa.Numeric(precision=18, scale=6), nullable=True),
    sa.Column('delta', sa.Numeric(precision=18, scale=6), nullable=True),
    sa.Column('gamma', sa.Numeric(precision=18, scale=6), nullable=True),
    sa.Column('theta', sa.Numeric(precision=18, scale=6), nullable=True),
    sa.Column('vega', sa.Numeric(precision=18, scale=6), nullable=True),
    sa.Column('capture_error', sa.Text(), nullable=True),
    sa.Column('source_provider', sa.String(length=64), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['decision_id'], ['decision_snapshot.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.add_column(
        'entry_snapshot',
        sa.Column(
            'option_type',
            sa.Enum('CALL', 'PUT', name='option_type', create_type=False),
            nullable=True,
        ),
    )
    # Required index (decision_id, for the FK/join pattern every read uses).
    op.create_index(op.f('ix_entry_snapshot_decision_id'), 'entry_snapshot', ['decision_id'], unique=False)
    op.create_index(op.f('ix_entry_snapshot_captured_at'), 'entry_snapshot', ['captured_at'], unique=False)

    # "No overwrite updates" (Phase 4.1 brief, entry_snapshot requirement)
    # enforced as a real DB guarantee, not just a service-layer convention
    # -- Phase 4.1 has no service code yet to hold that convention. A
    # retry after a failed capture must INSERT a new row; this trigger
    # makes an accidental UPDATE fail loudly instead of silently
    # corrupting the audit trail. Shared by settlement_snapshot (see
    # 24e13c12e7dc), which reuses this same function rather than defining
    # its own.
    op.execute(
        """
        CREATE FUNCTION reject_snapshot_update() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION
                'Phase 4 snapshot rows are insert-only (immutable audit trail): '
                '% row id=% cannot be UPDATEd -- INSERT a new row instead',
                TG_TABLE_NAME, OLD.id;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER entry_snapshot_no_update
        BEFORE UPDATE ON entry_snapshot
        FOR EACH ROW EXECUTE FUNCTION reject_snapshot_update();
        """
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP TRIGGER IF EXISTS entry_snapshot_no_update ON entry_snapshot")
    op.drop_index(op.f('ix_entry_snapshot_captured_at'), table_name='entry_snapshot')
    op.drop_index(op.f('ix_entry_snapshot_decision_id'), table_name='entry_snapshot')
    op.drop_table('entry_snapshot')
    # reject_snapshot_update() is dropped here, not in settlement_snapshot's
    # migration -- by the time this downgrade runs, settlement_snapshot's
    # own downgrade (which also depends on this function) has already run,
    # since downgrades apply in reverse order.
    op.execute("DROP FUNCTION IF EXISTS reject_snapshot_update()")
    # option_type is NOT dropped here -- it's owned by c535e22e3a6d
    # (options_snapshot), which still uses it.
    sa.Enum(name='option_action').drop(op.get_bind(), checkfirst=True)
    sa.Enum(name='capture_status').drop(op.get_bind(), checkfirst=True)
