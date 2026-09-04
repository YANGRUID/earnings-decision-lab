"""allow an appended recovery settlement to supersede a failed attempt

A configuration settlement was write-once AND one-per-configuration: the
UPDATE trigger rejects rewrites, and uq_v4_shadow_config_settlement_one_
per_config allowed exactly one row per configuration ever. Together those
made a failed settlement permanent -- the position could never be settled
later even when real market data existed for it, which is exactly what the
2026-09-04 required-side incident produced across 27 configurations.

The immutability itself is right and is kept. What changes is the
uniqueness: a configuration may now carry several settlement ATTEMPTS, of
which at most one may be SETTLED -- enforced by a partial unique index, so
"no duplicate successful settlement" becomes a database guarantee rather
than an application convention. The original failed row stays exactly as
written, and the recovery row points back at it.

Revision ID: d1f3a5c7e9b2
Revises: c9e1b3d5f7a9
"""

import sqlalchemy as sa
from alembic import op

revision = "d1f3a5c7e9b2"
down_revision = "c9e1b3d5f7a9"
branch_labels = None
depends_on = None

_TABLE = "v4_shadow_config_settlement"
_OLD_UNIQUE = "uq_v4_shadow_config_settlement_one_per_config"
_NEW_UNIQUE = "uq_v4_shadow_config_settlement_one_settled_per_config"


def upgrade() -> None:
    op.drop_constraint(_OLD_UNIQUE, _TABLE, type_="unique")
    op.create_index(
        _NEW_UNIQUE,
        _TABLE,
        ["shadow_config_result_id"],
        unique=True,
        postgresql_where=sa.text("status = 'SETTLED'"),
    )
    op.add_column(_TABLE, sa.Column("pricing_method", sa.String(length=96), nullable=True))
    op.add_column(_TABLE, sa.Column("recovery_provenance", sa.String(length=48), nullable=True))
    op.add_column(
        _TABLE, sa.Column("supersedes_settlement_id", sa.Integer(), nullable=True)
    )
    op.create_foreign_key(
        "v4_shadow_config_settlement_supersedes_fkey",
        _TABLE,
        _TABLE,
        ["supersedes_settlement_id"],
        ["id"],
    )


def downgrade() -> None:
    # Only reversible while no configuration actually carries more than one
    # settlement attempt -- restoring the blanket unique constraint over
    # real superseded history would fail, and silently deleting that history
    # to make it succeed is not something a downgrade may do.
    bind = op.get_bind()
    duplicates = bind.execute(
        sa.text(
            f"SELECT count(*) FROM (SELECT shadow_config_result_id FROM {_TABLE} "
            "GROUP BY shadow_config_result_id HAVING count(*) > 1) d"
        )
    ).scalar()
    if duplicates:
        raise RuntimeError(
            f"{duplicates} configuration(s) carry superseded settlement history; "
            "restoring the one-row-per-configuration constraint would require "
            "destroying real settlement evidence"
        )
    op.drop_constraint("v4_shadow_config_settlement_supersedes_fkey", _TABLE, type_="foreignkey")
    op.drop_column(_TABLE, "supersedes_settlement_id")
    op.drop_column(_TABLE, "recovery_provenance")
    op.drop_column(_TABLE, "pricing_method")
    op.drop_index(_NEW_UNIQUE, table_name=_TABLE)
    op.create_unique_constraint(_OLD_UNIQUE, _TABLE, ["shadow_config_result_id"])
