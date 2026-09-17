"""earnings calendar: corroboration provenance, profile freshness, verification

Additive only, and hand-written: ``alembic revision --autogenerate`` has, in
this repository, proposed dropping ``apscheduler_jobs``, the RAG HNSW index and
the FTS index. Seven nullable columns on ``earnings_calendar_event``; nothing is
dropped, renamed or rewritten, and no existing row changes value.

Why (2026-09-17 audit). The calendar has two providers of very different
reliability and the table recorded only which one last wrote a row. That was
not enough to reconcile them safely:

* ``last_confirmed_by`` / ``last_confirmed_at`` -- which provider most recently
  listed the event on its stored date. A less authoritative provider may not
  overrule a more authoritative confirmation (date, timing or existence).
  Without it, an EarningsAPI quota outage that pushed the sync onto Finnhub
  marked 65 EarningsAPI-confirmed events -- KR and TCOM among them -- as
  vanished, because Finnhub simply did not list them.
* ``vanished_by`` / ``vanished_at`` -- which provider's successful answer
  omitted the event, so only an equally authoritative provider can restore it.
* ``profile_refreshed_at`` -- when the company profile (name, market cap,
  country) was last fetched. The sync re-fetched every profile on every run,
  roughly 80 EarningsAPI requests a day against a 1,000-a-month free plan,
  which was exhausted on 2026-09-13.
* ``verified_at`` / ``verification_note`` -- an operator-verified date and
  timing, with its source. Provider data never changes a verified row; a
  disagreement is reported as a conflict instead. Needed because the only
  working provider was verified wrong for several eligible events (a phantom
  FERG report, GIS on the wrong date, ACN as after-close when it reports
  before the open).

Revision ID: a4c6e8b0d2f4
Revises: f7a9c1e3d5b8
"""

import sqlalchemy as sa
from alembic import op

revision = "a4c6e8b0d2f4"
down_revision = "f7a9c1e3d5b8"
branch_labels = None
depends_on = None

_TABLE = "earnings_calendar_event"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("last_confirmed_by", sa.String(length=32), nullable=True))
    op.add_column(_TABLE, sa.Column("last_confirmed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(_TABLE, sa.Column("vanished_by", sa.String(length=32), nullable=True))
    op.add_column(_TABLE, sa.Column("vanished_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        _TABLE, sa.Column("profile_refreshed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(_TABLE, sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(_TABLE, sa.Column("verification_note", sa.Text(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    verified = bind.exec_driver_sql(
        f"SELECT count(*) FROM {_TABLE} WHERE verified_at IS NOT NULL"
    ).scalar()
    if verified:
        raise RuntimeError(
            f"refusing to downgrade: {verified} operator-verified calendar row(s) would lose "
            "their verification evidence. Export them first."
        )
    for column in (
        "verification_note",
        "verified_at",
        "profile_refreshed_at",
        "vanished_at",
        "vanished_by",
        "last_confirmed_at",
        "last_confirmed_by",
    ):
        op.drop_column(_TABLE, column)
