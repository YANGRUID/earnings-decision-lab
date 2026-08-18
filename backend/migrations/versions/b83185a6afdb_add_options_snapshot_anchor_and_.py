"""add options snapshot anchor and earnings date provenance

Revision ID: b83185a6afdb
Revises: b412b07bf06d
Create Date: 2026-08-18 14:14:21.167167

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b83185a6afdb'
down_revision: Union[str, Sequence[str], None] = 'b412b07bf06d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # NOTE: autogenerate also wants to drop ix_document_chunk_embedding_hnsw
    # and ix_document_chunk_text_fts every time -- both are raw-SQL indexes
    # autogenerate can't see as ORM-managed. Manually stripped; see prior
    # migrations.

    # Every options_snapshot/volatility_snapshot row that exists before this
    # migration was collected under the old earnings-date-required pipeline
    # -- real, correct backfill, not a guess. Added nullable first because
    # Postgres can't add a NOT NULL column with no default to a populated
    # table in one step.
    anchor_enum = sa.Enum('EARNINGS_ANCHORED', 'GENERAL_CURRENT', name='options_snapshot_anchor')
    anchor_enum.create(op.get_bind(), checkfirst=True)

    op.add_column(
        'options_snapshot',
        sa.Column(
            'anchor',
            sa.Enum(
                'EARNINGS_ANCHORED', 'GENERAL_CURRENT',
                name='options_snapshot_anchor', create_type=False,
            ),
            nullable=True,
        ),
    )
    op.execute("UPDATE options_snapshot SET anchor = 'EARNINGS_ANCHORED' WHERE anchor IS NULL")
    op.alter_column('options_snapshot', 'anchor', nullable=False)

    op.add_column(
        'volatility_snapshot',
        sa.Column(
            'anchor',
            sa.Enum(
                'EARNINGS_ANCHORED', 'GENERAL_CURRENT',
                name='options_snapshot_anchor', create_type=False,
            ),
            nullable=True,
        ),
    )
    op.execute("UPDATE volatility_snapshot SET anchor = 'EARNINGS_ANCHORED' WHERE anchor IS NULL")
    op.alter_column('volatility_snapshot', 'anchor', nullable=False)

    # Same reasoning: every existing earnings_estimate_snapshot row was
    # collected via Alpha Vantage -- real backfill for real historical rows.
    date_source_enum = sa.Enum(
        'ALPHA_VANTAGE', 'MANUAL', 'ESTIMATED', 'UNKNOWN', name='upcoming_earnings_date_source'
    )
    date_source_enum.create(op.get_bind(), checkfirst=True)
    op.add_column(
        'earnings_estimate_snapshot',
        sa.Column(
            'date_source',
            sa.Enum(
                'ALPHA_VANTAGE', 'MANUAL', 'ESTIMATED', 'UNKNOWN',
                name='upcoming_earnings_date_source', create_type=False,
            ),
            nullable=True,
        ),
    )
    op.execute(
        "UPDATE earnings_estimate_snapshot SET date_source = 'ALPHA_VANTAGE' "
        "WHERE date_source IS NULL"
    )
    op.alter_column('earnings_estimate_snapshot', 'date_source', nullable=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('earnings_estimate_snapshot', 'date_source')
    sa.Enum(name='upcoming_earnings_date_source').drop(op.get_bind(), checkfirst=True)

    op.drop_column('volatility_snapshot', 'anchor')
    op.drop_column('options_snapshot', 'anchor')
    sa.Enum(name='options_snapshot_anchor').drop(op.get_bind(), checkfirst=True)
