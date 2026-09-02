"""add decision snapshot reproducibility fields

Revision ID: 7125ac88b7c7
Revises: 00b397af8ecb
Create Date: 2026-08-26 19:19:37.902771

Phase 4 reproducibility hardening (2026-08-26), Section 2 -- six new,
nullable, additive columns on the immutable decision_snapshot table. NULL
on every historical row (including every Aug 25 row), populated from
here forward by services/decision_snapshot_freezing.py. Never touches an
existing row's value: only ADD COLUMN, no UPDATE/backfill of any kind, so
the decision_snapshot_no_update trigger is never implicated (Postgres
ADD COLUMN with no default is a catalog-only change, not a row rewrite).

``decision_volatility_view`` and ``risk_profile`` are both pre-existing
enum TYPES already created by ai_decision_version's and
benchmark_portfolio's own earlier migrations respectively -- reused here
with ``create_type=False``, the same established workaround this
project's migrations already use for ``option_type`` (see
entry_capture_attempt's and quote_acquisition_attempt's own migrations)
to avoid Postgres's DuplicateObject error on a type that already exists.

Autogenerate also picked up unrelated, pre-existing schema drift
(apscheduler's own runtime-created ``apscheduler_jobs`` table, and two
document_chunk indexes not tracked by any ORM model) -- stripped out
here, same as the prior quote_acquisition_attempt migration, since this
revision's own job is only the decision_snapshot columns above.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '7125ac88b7c7'
down_revision: Union[str, Sequence[str], None] = '00b397af8ecb'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'decision_snapshot',
        sa.Column(
            'volatility_view',
            sa.Enum(
                'LONG_VOL', 'NEUTRAL_VOL', 'SHORT_VOL',
                name='decision_volatility_view', create_type=False,
            ),
            nullable=True,
        ),
    )
    op.add_column(
        'decision_snapshot',
        sa.Column(
            'effective_risk_profile',
            sa.Enum(
                'CONSERVATIVE', 'MODERATE', 'AGGRESSIVE',
                name='risk_profile', create_type=False,
            ),
            nullable=True,
        ),
    )
    op.add_column(
        'decision_snapshot', sa.Column('deterministic_confidence_score', sa.Integer(), nullable=True)
    )
    op.add_column(
        'decision_snapshot',
        sa.Column('deterministic_confidence_breakdown', sa.JSON(), nullable=True),
    )
    op.add_column(
        'decision_snapshot', sa.Column('decision_llm_provider', sa.String(length=64), nullable=True)
    )
    op.add_column(
        'decision_snapshot', sa.Column('decision_llm_model', sa.String(length=128), nullable=True)
    )


def downgrade() -> None:
    op.drop_column('decision_snapshot', 'decision_llm_model')
    op.drop_column('decision_snapshot', 'decision_llm_provider')
    op.drop_column('decision_snapshot', 'deterministic_confidence_breakdown')
    op.drop_column('decision_snapshot', 'deterministic_confidence_score')
    op.drop_column('decision_snapshot', 'effective_risk_profile')
    op.drop_column('decision_snapshot', 'volatility_view')
