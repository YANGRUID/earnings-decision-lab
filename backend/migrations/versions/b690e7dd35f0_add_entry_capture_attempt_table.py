"""add entry_capture_attempt table

Revision ID: b690e7dd35f0
Revises: 641899980b94
Create Date: 2026-08-21 12:49:49.439168

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b690e7dd35f0'
down_revision: Union[str, Sequence[str], None] = '641899980b94'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # `capture_status` reuses the existing enum type created by
    # 78ee400f83ab (entry_snapshot). Added via a separate add_column step
    # below (not inline in create_table) -- confirmed in Phase 4.1
    # (decision_snapshot's own migration) that inline reuse of an
    # already-existing enum type inside create_table does not reliably
    # honor create_type=False in this SQLAlchemy/psycopg combination.
    op.create_table('entry_capture_attempt',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('decision_snapshot_id', sa.Integer(), nullable=False),
    sa.Column('benchmark_portfolio_id', sa.Integer(), nullable=False),
    sa.Column('capture_error', sa.Text(), nullable=True),
    sa.Column('underlying_price', sa.Numeric(precision=18, scale=6), nullable=True),
    sa.Column('underlying_bid', sa.Numeric(precision=18, scale=6), nullable=True),
    sa.Column('underlying_ask', sa.Numeric(precision=18, scale=6), nullable=True),
    sa.Column('underlying_timestamp', sa.DateTime(timezone=True), nullable=True),
    sa.Column('option_market_timestamp', sa.DateTime(timezone=True), nullable=True),
    sa.Column('net_entry_price_per_share', sa.Numeric(precision=18, scale=6), nullable=True),
    sa.Column('net_entry_cash', sa.Numeric(precision=18, scale=6), nullable=True),
    sa.Column('contracts', sa.Integer(), nullable=True),
    sa.Column('initial_max_risk', sa.Numeric(precision=18, scale=6), nullable=True),
    sa.Column('capital_utilization', sa.Numeric(precision=18, scale=6), nullable=True),
    sa.Column('source_provider', sa.String(length=64), nullable=True),
    sa.Column('captured_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['benchmark_portfolio_id'], ['benchmark_portfolio.id'], ),
    sa.ForeignKeyConstraint(['decision_snapshot_id'], ['decision_snapshot.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.add_column(
        'entry_capture_attempt',
        sa.Column(
            'status',
            sa.Enum('PENDING', 'CAPTURED', 'FAILED', 'SKIPPED', name='capture_status', create_type=False),
            nullable=False,
        ),
    )
    op.create_index(
        op.f('ix_entry_capture_attempt_benchmark_portfolio_id'),
        'entry_capture_attempt', ['benchmark_portfolio_id'], unique=False,
    )
    op.create_index(
        op.f('ix_entry_capture_attempt_decision_snapshot_id'),
        'entry_capture_attempt', ['decision_snapshot_id'], unique=False,
    )
    # The real, DB-level idempotency guarantee (Phase 4.4 sec 15): at
    # most one status=CAPTURED attempt per (decision_snapshot_id,
    # benchmark_portfolio_id). FAILED/PENDING rows are unrestricted.
    op.create_index(
        'uq_entry_capture_attempt_one_captured_per_decision_portfolio',
        'entry_capture_attempt', ['decision_snapshot_id', 'benchmark_portfolio_id'],
        unique=True, postgresql_where=sa.text("status = 'CAPTURED'"),
    )

    # Phase 4.4 sec 0A/4.1's immutability precedent: reuses the exact
    # same trigger function entry_snapshot already installed
    # (78ee400f83ab) -- this row is written exactly once, fully formed,
    # after an attempt concludes (success or failure), never updated.
    op.execute(
        """
        CREATE TRIGGER entry_capture_attempt_no_update
        BEFORE UPDATE ON entry_capture_attempt
        FOR EACH ROW EXECUTE FUNCTION reject_snapshot_update();
        """
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(
        "DROP TRIGGER IF EXISTS entry_capture_attempt_no_update ON entry_capture_attempt"
    )
    op.drop_index(
        'uq_entry_capture_attempt_one_captured_per_decision_portfolio',
        table_name='entry_capture_attempt', postgresql_where=sa.text("status = 'CAPTURED'"),
    )
    op.drop_index(
        op.f('ix_entry_capture_attempt_decision_snapshot_id'), table_name='entry_capture_attempt'
    )
    op.drop_index(
        op.f('ix_entry_capture_attempt_benchmark_portfolio_id'), table_name='entry_capture_attempt'
    )
    op.drop_table('entry_capture_attempt')
    # capture_status is NOT dropped here -- it's owned by 78ee400f83ab
    # (entry_snapshot), which still uses it.
