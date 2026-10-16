"""widen the observation phase column so a late recovery can name itself

Hand-written: ``alembic revision --autogenerate`` has, in this repository,
proposed dropping ``apscheduler_jobs``, the RAG HNSW index and the FTS index.

Two columns widened, ``VARCHAR(8) -> VARCHAR(24)``, on
``v4_shadow_candidate_observation.phase`` and
``v4_2_challenger_candidate_observation.phase``. Nothing is dropped, nothing
is renamed, and no existing row changes value: in PostgreSQL, widening a
``varchar(n)`` is a catalog-only change and does not rewrite the table.

Why (2026-10-16). The emergency settlement recovery authorized today has to
persist its own per-leg closing-mark provenance, and a unique index allows
exactly one observation per (decision, candidate, phase). Every candidate
stranded by the 2026-10-13 gateway outage already had an ``EXIT_EOD`` row --
the failed 16:30 attempt -- so the recovery had nowhere to write. It silently
reused that row instead: eleven settled JNJ rows ended up pointing at an
observation that said NOT_EXECUTABLE and carried no prices at all.

A second phase, ``EXIT_LATE_RECOVERY``, fixes that, and it does not fit in
eight characters. The alternative was a cryptic seven-character code, which
is a poor thing to stamp on evidence an operator has to read, or rewriting
the earlier attempt's row, which is exactly what append-only forbids.

The downgrade truncates back to 8 and will fail if any
``EXIT_LATE_RECOVERY`` row exists, which is correct: that row is forward
evidence, and silently truncating it to ``EXIT_LAT`` would corrupt it.

Revision ID: d1f3b8a46e27
Revises: c9a2e71b53d8
"""

import sqlalchemy as sa
from alembic import op

revision = "d1f3b8a46e27"
down_revision = "c9a2e71b53d8"
branch_labels = None
depends_on = None

_TABLES = (
    "v4_shadow_candidate_observation",
    "v4_2_challenger_candidate_observation",
)


def upgrade() -> None:
    for table in _TABLES:
        op.alter_column(
            table,
            "phase",
            existing_type=sa.String(length=8),
            type_=sa.String(length=24),
            existing_nullable=False,
        )


def downgrade() -> None:
    for table in _TABLES:
        op.alter_column(
            table,
            "phase",
            existing_type=sa.String(length=24),
            type_=sa.String(length=8),
            existing_nullable=False,
        )
