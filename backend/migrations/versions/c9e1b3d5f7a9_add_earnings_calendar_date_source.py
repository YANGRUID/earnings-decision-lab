"""Earnings estimates: the earnings-calendar provider becomes the primary source.

Revision ID: c9e1b3d5f7a9
Revises: b7d9f1a3c5e7
Create Date: 2026-09-03

Adds ``EARNINGS_CALENDAR`` to the ``upcoming_earnings_date_source`` enum so an
EarningsEstimateSnapshot can honestly record that its report date and
consensus came from the earnings calendar (EarningsAPI / Finnhub) rather than
from Alpha Vantage. Nothing is rewritten; existing rows keep their provenance.
"""

from alembic import op

revision = "c9e1b3d5f7a9"
down_revision = "b7d9f1a3c5e7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # PostgreSQL 12+ accepts ADD VALUE inside a transaction; the new value is
    # simply not usable until the transaction commits, which is fine here.
    op.execute(
        "ALTER TYPE upcoming_earnings_date_source ADD VALUE IF NOT EXISTS 'EARNINGS_CALENDAR'"
    )


def downgrade() -> None:
    # PostgreSQL cannot drop a single enum value; the label stays. Rows that use
    # it would have to be deleted first, which this migration refuses to do.
    raise RuntimeError("EARNINGS_CALENDAR cannot be removed from the enum; leave it in place")
