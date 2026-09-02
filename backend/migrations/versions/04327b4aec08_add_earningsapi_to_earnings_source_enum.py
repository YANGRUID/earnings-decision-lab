"""add earningsapi to earnings_source enum

Revision ID: 04327b4aec08
Revises: c213d3d195d0
Create Date: 2026-08-25 08:30:10.493198

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '04327b4aec08'
down_revision: Union[str, Sequence[str], None] = 'c213d3d195d0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # EarningsAPI.com became the primary earnings-calendar source (Finnhub
    # demoted to fallback) -- see EARNINGS_CALENDAR_PROVIDER_ARCHITECTURE_
    # REVIEW.md. Same real pattern as 796745399657 (OptionsSnapshotPurpose):
    # the existing 'FINNHUB' label is the Python Enum member *name*, not
    # its .value ("finnhub") -- this column was declared without
    # values_callable, so SQLAlchemy maps by .name. Postgres requires
    # ALTER TYPE ... ADD VALUE to run outside an explicit transaction
    # block in older versions; autocommit_block() keeps this safe to run
    # standalone or as part of the normal alembic upgrade chain.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE earnings_source ADD VALUE IF NOT EXISTS 'EARNINGSAPI'")


def downgrade() -> None:
    """Downgrade schema."""
    # Postgres cannot drop individual enum values -- the new EARNINGSAPI
    # member is left in place on downgrade (harmless: nothing writes it
    # once the app code that references it is gone).
    pass
