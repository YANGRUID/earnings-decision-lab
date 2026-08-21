"""add settlement_capture_attempt and exit_snapshot tables

Revision ID: c213d3d195d0
Revises: 70d3fedfd3f4
Create Date: 2026-08-21 15:11:10.308022

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c213d3d195d0'
down_revision: Union[str, Sequence[str], None] = '70d3fedfd3f4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Phase 4.5 -- settlement_capture_attempt / exit_snapshot mirror
    # entry_capture_attempt / entry_snapshot (Phase 4.4) exactly, on the
    # closing side. The pre-existing settlement_snapshot table (Phase
    # 4.1 scaffold) is deliberately left untouched by this migration --
    # see PHASE4_5_SETTLEMENT_ARCHITECTURE_REVIEW.md's 2026-08-21
    # addendum for the full reasoning.
    #
    # `capture_status`, `option_type`, `option_action`, and
    # `market_data_quality` all already exist (created by earlier Phase
    # 4 migrations) -- reused via a separate add_column step per table,
    # not inline inside create_table, since inline reuse of an
    # already-existing enum type does not reliably honor
    # create_type=False in this SQLAlchemy/psycopg combination
    # (confirmed repeatedly: decision_snapshot's own migration, and
    # again for entry_capture_attempt).
    op.create_table('settlement_capture_attempt',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('decision_snapshot_id', sa.Integer(), nullable=False),
    sa.Column('benchmark_portfolio_id', sa.Integer(), nullable=False),
    sa.Column('entry_capture_attempt_id', sa.Integer(), nullable=True),
    sa.Column('capture_error', sa.Text(), nullable=True),
    sa.Column('underlying_price', sa.Numeric(precision=18, scale=6), nullable=True),
    sa.Column('underlying_bid', sa.Numeric(precision=18, scale=6), nullable=True),
    sa.Column('underlying_ask', sa.Numeric(precision=18, scale=6), nullable=True),
    sa.Column('underlying_timestamp', sa.DateTime(timezone=True), nullable=True),
    sa.Column('exit_market_timestamp', sa.DateTime(timezone=True), nullable=True),
    sa.Column('net_exit_price_per_share', sa.Numeric(precision=18, scale=6), nullable=True),
    sa.Column('net_exit_cash', sa.Numeric(precision=18, scale=6), nullable=True),
    sa.Column('realized_pnl', sa.Numeric(precision=18, scale=6), nullable=True),
    sa.Column('return_pct', sa.Numeric(precision=18, scale=6), nullable=True),
    sa.Column('r_multiple', sa.Numeric(precision=18, scale=6), nullable=True),
    sa.Column('is_win', sa.Boolean(), nullable=True),
    sa.Column('source_provider', sa.String(length=64), nullable=True),
    sa.Column('captured_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['benchmark_portfolio_id'], ['benchmark_portfolio.id'], ),
    sa.ForeignKeyConstraint(['decision_snapshot_id'], ['decision_snapshot.id'], ),
    sa.ForeignKeyConstraint(['entry_capture_attempt_id'], ['entry_capture_attempt.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.add_column(
        'settlement_capture_attempt',
        sa.Column(
            'status',
            sa.Enum('PENDING', 'CAPTURED', 'FAILED', 'SKIPPED', name='capture_status', create_type=False),
            nullable=False,
        ),
    )
    op.create_index(op.f('ix_settlement_capture_attempt_benchmark_portfolio_id'), 'settlement_capture_attempt', ['benchmark_portfolio_id'], unique=False)
    op.create_index(op.f('ix_settlement_capture_attempt_decision_snapshot_id'), 'settlement_capture_attempt', ['decision_snapshot_id'], unique=False)
    op.create_index(op.f('ix_settlement_capture_attempt_entry_capture_attempt_id'), 'settlement_capture_attempt', ['entry_capture_attempt_id'], unique=False)
    # The real, DB-level idempotency guarantee, mirroring entry_capture_
    # attempt's own: at most one status=CAPTURED settlement per
    # (decision_snapshot_id, benchmark_portfolio_id). FAILED/PENDING
    # rows are unrestricted -- retries after a failure are always
    # allowed.
    op.create_index(
        'uq_settlement_attempt_one_captured_per_decision_portfolio',
        'settlement_capture_attempt', ['decision_snapshot_id', 'benchmark_portfolio_id'],
        unique=True, postgresql_where=sa.text("status = 'CAPTURED'"),
    )
    # Immutability -- reuses the exact same trigger function
    # entry_snapshot/settlement_snapshot/entry_capture_attempt already
    # installed (78ee400f83ab); this row is written exactly once, fully
    # formed, after an exit attempt concludes (success or failure),
    # never updated.
    op.execute(
        """
        CREATE TRIGGER settlement_capture_attempt_no_update
        BEFORE UPDATE ON settlement_capture_attempt
        FOR EACH ROW EXECUTE FUNCTION reject_snapshot_update();
        """
    )

    op.create_table('exit_snapshot',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('decision_id', sa.Integer(), nullable=False),
    sa.Column('settlement_attempt_id', sa.Integer(), nullable=False),
    sa.Column('entry_snapshot_id', sa.Integer(), nullable=False),
    sa.Column('leg_index', sa.Integer(), nullable=False),
    sa.Column('captured_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('external_contract_id', sa.String(length=32), nullable=True),
    sa.Column('expiration', sa.Date(), nullable=True),
    sa.Column('strike', sa.Numeric(precision=18, scale=6), nullable=True),
    sa.Column('quantity', sa.Integer(), nullable=True),
    sa.Column('multiplier', sa.Numeric(precision=18, scale=6), nullable=True),
    sa.Column('bid', sa.Numeric(precision=18, scale=6), nullable=True),
    sa.Column('ask', sa.Numeric(precision=18, scale=6), nullable=True),
    sa.Column('mid', sa.Numeric(precision=18, scale=6), nullable=True),
    sa.Column('last_price', sa.Numeric(precision=18, scale=6), nullable=True),
    sa.Column('implied_volatility', sa.Numeric(precision=18, scale=6), nullable=True),
    sa.Column('delta', sa.Numeric(precision=18, scale=6), nullable=True),
    sa.Column('gamma', sa.Numeric(precision=18, scale=6), nullable=True),
    sa.Column('theta', sa.Numeric(precision=18, scale=6), nullable=True),
    sa.Column('vega', sa.Numeric(precision=18, scale=6), nullable=True),
    sa.Column('pricing_source', sa.String(length=32), nullable=True),
    sa.Column('benchmark_exit_price', sa.Numeric(precision=18, scale=6), nullable=True),
    sa.Column('pricing_assumption', sa.String(length=32), nullable=True),
    sa.Column('realized_pnl_per_share', sa.Numeric(precision=18, scale=6), nullable=True),
    sa.Column('capture_error', sa.Text(), nullable=True),
    sa.Column('source_provider', sa.String(length=64), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['decision_id'], ['decision_snapshot.id'], ),
    sa.ForeignKeyConstraint(['entry_snapshot_id'], ['entry_snapshot.id'], ),
    sa.ForeignKeyConstraint(['settlement_attempt_id'], ['settlement_capture_attempt.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('settlement_attempt_id', 'leg_index', name='uq_exit_snapshot_attempt_leg')
    )
    op.add_column(
        'exit_snapshot',
        sa.Column(
            'status',
            sa.Enum('PENDING', 'CAPTURED', 'FAILED', 'SKIPPED', name='capture_status', create_type=False),
            nullable=False,
        ),
    )
    op.add_column(
        'exit_snapshot',
        sa.Column(
            'option_type',
            sa.Enum('CALL', 'PUT', name='option_type', create_type=False),
            nullable=True,
        ),
    )
    op.add_column(
        'exit_snapshot',
        sa.Column(
            'action',
            sa.Enum('BUY', 'SELL', name='option_action', create_type=False),
            nullable=True,
        ),
    )
    op.add_column(
        'exit_snapshot',
        sa.Column(
            'market_data_quality',
            sa.Enum('LIVE', 'DELAYED', 'FROZEN', 'UNKNOWN', name='market_data_quality', create_type=False),
            nullable=True,
        ),
    )
    op.create_index(op.f('ix_exit_snapshot_captured_at'), 'exit_snapshot', ['captured_at'], unique=False)
    op.create_index(op.f('ix_exit_snapshot_decision_id'), 'exit_snapshot', ['decision_id'], unique=False)
    op.create_index(op.f('ix_exit_snapshot_entry_snapshot_id'), 'exit_snapshot', ['entry_snapshot_id'], unique=False)
    op.create_index(op.f('ix_exit_snapshot_settlement_attempt_id'), 'exit_snapshot', ['settlement_attempt_id'], unique=False)
    op.execute(
        """
        CREATE TRIGGER exit_snapshot_no_update
        BEFORE UPDATE ON exit_snapshot
        FOR EACH ROW EXECUTE FUNCTION reject_snapshot_update();
        """
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP TRIGGER IF EXISTS exit_snapshot_no_update ON exit_snapshot")
    op.drop_index(op.f('ix_exit_snapshot_settlement_attempt_id'), table_name='exit_snapshot')
    op.drop_index(op.f('ix_exit_snapshot_entry_snapshot_id'), table_name='exit_snapshot')
    op.drop_index(op.f('ix_exit_snapshot_decision_id'), table_name='exit_snapshot')
    op.drop_index(op.f('ix_exit_snapshot_captured_at'), table_name='exit_snapshot')
    op.drop_table('exit_snapshot')
    # capture_status/option_type/option_action/market_data_quality are
    # NOT dropped here -- all four are owned by earlier migrations whose
    # tables still use them.

    op.execute(
        "DROP TRIGGER IF EXISTS settlement_capture_attempt_no_update ON settlement_capture_attempt"
    )
    op.drop_index(
        'uq_settlement_attempt_one_captured_per_decision_portfolio',
        table_name='settlement_capture_attempt', postgresql_where=sa.text("status = 'CAPTURED'"),
    )
    op.drop_index(op.f('ix_settlement_capture_attempt_entry_capture_attempt_id'), table_name='settlement_capture_attempt')
    op.drop_index(op.f('ix_settlement_capture_attempt_decision_snapshot_id'), table_name='settlement_capture_attempt')
    op.drop_index(op.f('ix_settlement_capture_attempt_benchmark_portfolio_id'), table_name='settlement_capture_attempt')
    op.drop_table('settlement_capture_attempt')
