"""add unavailable to market data quality enum

Revision ID: 44917de38fb4
Revises: 7125ac88b7c7
Create Date: 2026-08-26 19:25:34.439235

Phase 4 market-data-quality hardening (2026-08-26), Section 15 -- adds the
real, previously-missing UNAVAILABLE value to the market_data_quality
Postgres enum. IBKR's own "N" (Not Subscribed) availability code already
decodes to the Python-side string "unavailable" (providers/ibkr_client.py's
decode_market_data_quality), and MarketDataQuality("unavailable") now
resolves to the MarketDataQuality.UNAVAILABLE member -- but every DB-mapped
enum in this project is declared as ``Enum(SomeEnum, name=...)`` with no
``values_callable`` override, so SQLAlchemy's default behavior persists
the Python member's *name* (uppercase, e.g. "LIVE"), never its lowercase
``.value``. The new label added here must match that same existing
convention exactly (UPPERCASE), not the lowercase provider string, or a
real UNAVAILABLE row would fail to persist for a different reason than
the one this migration exists to fix. Confirmed against this table's
existing real values (LIVE, DELAYED, FROZEN, UNKNOWN are all uppercase in
Postgres today) before writing this.

Never touches an existing row -- ADD VALUE only extends the type's valid
set, no existing value changes meaning, no row's stored value changes.
Postgres 12+ allows ADD VALUE inside a transaction (this project runs
PG16); the new label cannot be referenced by a DML statement in the same
transaction that adds it, which is fine here since this migration itself
never writes a row.

Downgrade is a documented no-op: Postgres has no native DROP VALUE for an
enum type (only a full type-recreation could remove it), and there is
nothing to reverse anyway -- no column, table, or row was added, only a
label some future row may or may not use.
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '44917de38fb4'
down_revision: Union[str, Sequence[str], None] = '7125ac88b7c7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE market_data_quality ADD VALUE IF NOT EXISTS 'UNAVAILABLE'")


def downgrade() -> None:
    # No native Postgres DROP VALUE for an enum label -- see module
    # docstring. Intentionally a no-op, not a raise: a downgrade that
    # left every other column change of this migration's siblings
    # reversed while refusing entirely here would be a worse outcome
    # than simply leaving one unused enum label in place.
    pass
