"""add earnings_calendar_event table

Revision ID: 27df376fc38c
Revises: c2a460e0545b
Create Date: 2026-08-20 22:48:46.707333

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '27df376fc38c'
down_revision: Union[str, Sequence[str], None] = 'c2a460e0545b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('earnings_calendar_event',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('symbol', sa.String(length=16), nullable=False),
    sa.Column('company_name', sa.String(length=255), nullable=False),
    sa.Column('logo_url', sa.String(length=512), nullable=True),
    sa.Column('earnings_date', sa.Date(), nullable=False),
    sa.Column('earnings_time', sa.Enum('BMO', 'AMC', 'DMH', 'UNKNOWN', name='earnings_timing'), nullable=False),
    sa.Column('eps_estimate', sa.Numeric(precision=18, scale=6), nullable=True),
    sa.Column('revenue_estimate', sa.Numeric(precision=18, scale=6), nullable=True),
    sa.Column('market_cap', sa.Numeric(precision=20, scale=2), nullable=True),
    sa.Column('country', sa.String(length=4), nullable=True),
    sa.Column('source', sa.Enum('FINNHUB', name='earnings_source'), nullable=False),
    sa.Column('status', sa.Enum('UPCOMING', 'ANALYZED', 'SKIPPED', 'COMPLETED', name='earnings_calendar_event_status'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('symbol', 'earnings_date', name='uq_earnings_calendar_event_symbol_date')
    )
    op.create_index(op.f('ix_earnings_calendar_event_earnings_date'), 'earnings_calendar_event', ['earnings_date'], unique=False)
    op.create_index(op.f('ix_earnings_calendar_event_status'), 'earnings_calendar_event', ['status'], unique=False)
    op.create_index(op.f('ix_earnings_calendar_event_symbol'), 'earnings_calendar_event', ['symbol'], unique=False)
    # NOTE: autogenerate also wants to drop ix_document_chunk_embedding_hnsw
    # and ix_document_chunk_text_fts every time -- both are raw-SQL indexes
    # autogenerate can't see as ORM-managed. Deliberately not touched here;
    # see prior migrations' same note.


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_earnings_calendar_event_symbol'), table_name='earnings_calendar_event')
    op.drop_index(op.f('ix_earnings_calendar_event_status'), table_name='earnings_calendar_event')
    op.drop_index(op.f('ix_earnings_calendar_event_earnings_date'), table_name='earnings_calendar_event')
    op.drop_table('earnings_calendar_event')
    sa.Enum(name='earnings_calendar_event_status').drop(op.get_bind(), checkfirst=True)
    sa.Enum(name='earnings_source').drop(op.get_bind(), checkfirst=True)
    sa.Enum(name='earnings_timing').drop(op.get_bind(), checkfirst=True)
