"""add option reconstruction fields and purpose enum values

Revision ID: 796745399657
Revises: 5315fa934655
Create Date: 2026-08-19 15:09:05.562113

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '796745399657'
down_revision: Union[str, Sequence[str], None] = '5315fa934655'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # New OptionsSnapshotPurpose members (Phase 14.13). The existing labels
    # in this Postgres enum are the Python Enum member *names*
    # (INTRADAY/CLOSE/MANUAL/EARNINGS), not their .value strings -- this
    # project's Enum(OptionsSnapshotPurpose, ...) column was declared
    # without values_callable, so SQLAlchemy maps by .name by default (see
    # OptionsSnapshot.purpose's own server_default=...INTRADAY.name).  The
    # new labels must match that same convention or the ORM will send
    # "NEAR_CLOSE"/"RECONSTRUCTED_CLOSE" and get a Postgres invalid-enum-
    # value error. Postgres requires ALTER TYPE ... ADD VALUE to run
    # outside an explicit transaction block in older versions; AUTOCOMMIT
    # keeps this migration safe to run standalone or as part of the normal
    # alembic upgrade chain.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE options_snapshot_purpose ADD VALUE IF NOT EXISTS 'NEAR_CLOSE'")
        op.execute(
            "ALTER TYPE options_snapshot_purpose ADD VALUE IF NOT EXISTS 'RECONSTRUCTED_CLOSE'"
        )

    op.add_column('options_snapshot', sa.Column('pricing_source', sa.String(length=32), nullable=True))
    op.add_column('options_snapshot', sa.Column('reconstruction_source', sa.String(length=32), nullable=True))
    op.add_column('options_snapshot', sa.Column('underlying_price', sa.Numeric(precision=18, scale=6), nullable=True))
    op.add_column('options_snapshot', sa.Column('underlying_timestamp', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    # Postgres cannot drop individual enum values -- the new
    # near_close/reconstructed_close members are left in place on
    # downgrade (harmless: nothing writes them once the app code that
    # references them is gone).
    op.drop_column('options_snapshot', 'underlying_timestamp')
    op.drop_column('options_snapshot', 'underlying_price')
    op.drop_column('options_snapshot', 'reconstruction_source')
    op.drop_column('options_snapshot', 'pricing_source')
