"""add quote_acquisition_attempt table

Revision ID: 00b397af8ecb
Revises: 656e38387005
Create Date: 2026-08-26 17:15:12.225542

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '00b397af8ecb'
down_revision: Union[str, Sequence[str], None] = '656e38387005'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    IBKR execution-observability hardening (2026-08-26): a new, separate
    operational/diagnostic table for real per-poll quote-acquisition
    telemetry (see models/quote_acquisition_attempt.py's own docstring
    for the full reasoning) -- additive only, no existing table is
    touched by this migration. Deliberately no immutability trigger
    (unlike entry_capture_attempt/entry_snapshot/settlement_capture_
    attempt/exit_snapshot): this is diagnostic evidence, not official
    trade evidence, and its append-only rule is enforced by never
    writing UPDATE code against it, not a DB-level guarantee.

    ``option_type`` (reusing the existing enum type already created by
    an earlier migration -- entry_snapshot) is added via a separate
    add_column step below, not inline in create_table -- confirmed by
    this project's own entry_capture_attempt migration (b690e7dd35f0)
    that inline reuse of an already-existing enum type inside
    create_table does not reliably honor create_type=False in this
    SQLAlchemy/psycopg combination; reconfirmed live here (a fresh
    attempt at inline reuse raised "type option_type already exists").
    """
    op.create_table(
        'quote_acquisition_attempt',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column(
            'capture_attempt_type',
            sa.Enum('ENTRY', 'SETTLEMENT', name='quote_acquisition_capture_type'),
            nullable=False,
        ),
        sa.Column('entry_capture_attempt_id', sa.Integer(), nullable=True),
        sa.Column('settlement_capture_attempt_id', sa.Integer(), nullable=True),
        sa.Column('ticker', sa.String(length=16), nullable=False),
        sa.Column('leg_index', sa.Integer(), nullable=True),
        sa.Column('expiration', sa.Date(), nullable=True),
        sa.Column('strike', sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column('external_contract_id', sa.String(length=32), nullable=True),
        sa.Column(
            'required_side',
            sa.Enum('ASK', 'BID', 'BID_ASK', 'ANALYTICAL', name='quote_requirement'),
            nullable=False,
        ),
        sa.Column('snapshot_attempt_number', sa.Integer(), nullable=False),
        sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('elapsed_ms', sa.Integer(), nullable=False),
        sa.Column('bid_present', sa.Boolean(), nullable=False),
        sa.Column('ask_present', sa.Boolean(), nullable=False),
        sa.Column('last_present', sa.Boolean(), nullable=False),
        sa.Column('bid', sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column('ask', sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column('last_price', sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column('market_data_quality', sa.String(length=16), nullable=True),
        sa.Column('rate_limited', sa.Boolean(), nullable=False),
        sa.Column('permission_error', sa.Boolean(), nullable=False),
        sa.Column('contract_resolved', sa.Boolean(), nullable=False),
        sa.Column('final_for_leg', sa.Boolean(), nullable=False),
        sa.Column(
            'created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(['entry_capture_attempt_id'], ['entry_capture_attempt.id'], ),
        sa.ForeignKeyConstraint(
            ['settlement_capture_attempt_id'], ['settlement_capture_attempt.id'],
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.add_column(
        'quote_acquisition_attempt',
        sa.Column(
            'option_type',
            sa.Enum('CALL', 'PUT', name='option_type', create_type=False),
            nullable=True,
        ),
    )
    op.create_index(
        op.f('ix_quote_acquisition_attempt_entry_capture_attempt_id'),
        'quote_acquisition_attempt', ['entry_capture_attempt_id'], unique=False,
    )
    op.create_index(
        op.f('ix_quote_acquisition_attempt_settlement_capture_attempt_id'),
        'quote_acquisition_attempt', ['settlement_capture_attempt_id'], unique=False,
    )
    op.create_index(
        op.f('ix_quote_acquisition_attempt_ticker'),
        'quote_acquisition_attempt', ['ticker'], unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        op.f('ix_quote_acquisition_attempt_ticker'), table_name='quote_acquisition_attempt',
    )
    op.drop_index(
        op.f('ix_quote_acquisition_attempt_settlement_capture_attempt_id'),
        table_name='quote_acquisition_attempt',
    )
    op.drop_index(
        op.f('ix_quote_acquisition_attempt_entry_capture_attempt_id'),
        table_name='quote_acquisition_attempt',
    )
    op.drop_table('quote_acquisition_attempt')
    op.execute("DROP TYPE IF EXISTS quote_acquisition_capture_type")
    op.execute("DROP TYPE IF EXISTS quote_requirement")
    # option_type is NOT dropped here -- it's owned by an earlier
    # migration (entry_snapshot), which still uses it.
