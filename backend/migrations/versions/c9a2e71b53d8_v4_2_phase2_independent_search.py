"""v4.2 phase 2: independent-search evidence

Hand-written: ``alembic revision --autogenerate`` has, in this repository,
proposed dropping ``apscheduler_jobs``, the RAG HNSW index and the FTS index.
Twenty-four nullable columns across three existing challenger tables, one new
index, and one idempotency constraint WIDENED. No table is dropped, no column
is dropped or renamed, and no existing row changes value.

The one non-additive step is deliberate and is explained under "the
idempotency constraint" below.

Why (2026-09-24). V4.2 Phase 1 reads the control's own frozen candidate rows,
so it searches the one expiry V4.1 chose and only the structures V4.1 kept
after its $2,000 standardized-capital screen. Phase 2 builds its own bounded
multi-expiry universe and lets each of the six configurations select within
it. That changes the search space and the selection unit, so the two phases'
rows are not one series and must never be concatenated.

``methodology_version`` is the discriminator, and it is deliberately NOT
backfilled. The 15 existing challenger decisions were written before Phase 2
existed; stamping them now would be writing a claim into frozen evidence that
the evidence never made. A null therefore means Phase 1, the shared-candidate
challenger, and every reader is expected to treat it that way.

The per-configuration columns exist because Phase 1's configuration rows
carry a status and a reason and nothing else, which is why 90 production rows
were indistinguishable from one another. Rank, quantity, capital used, max
risk used and the stage-by-stage rejection summary are what make one
configuration's answer auditable on its own terms.

Revision ID: c9a2e71b53d8
Revises: b7d1f4a92c60
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "c9a2e71b53d8"
down_revision = "b7d1f4a92c60"
branch_labels = None
depends_on = None

_DECISION = "v4_2_challenger_decision"
_CANDIDATE = "v4_2_challenger_candidate"
_CONFIG = "v4_2_challenger_config_result"

_JSON = postgresql.JSON(astext_type=sa.Text())

_DECISION_COLUMNS = (
    sa.Column("methodology_version", sa.String(length=48), nullable=True),
    sa.Column("candidate_universe_version", sa.String(length=48), nullable=True),
    sa.Column("configuration_version", sa.String(length=48), nullable=True),
    sa.Column("strategy_registry_version", sa.String(length=48), nullable=True),
    sa.Column("timing_policy_version", sa.String(length=64), nullable=True),
    sa.Column("configurations_actioned", sa.Integer(), nullable=True),
    sa.Column("distinct_selected_candidates", sa.Integer(), nullable=True),
    sa.Column("request_budget", _JSON, nullable=True),
)

_CANDIDATE_COLUMNS = (
    sa.Column("validity_status", sa.String(length=48), nullable=True),
    sa.Column("validity_reason", sa.Text(), nullable=True),
    sa.Column("per_contract_max_risk", sa.Numeric(18, 2), nullable=True),
    sa.Column("n_legs", sa.Integer(), nullable=True),
    sa.Column("n_legs_with_two_sided_quote", sa.Integer(), nullable=True),
)

_CONFIG_COLUMNS = (
    sa.Column("rank", sa.Integer(), nullable=True),
    sa.Column("quantity", sa.Integer(), nullable=True),
    sa.Column("per_contract_entry_cash", sa.Numeric(18, 2), nullable=True),
    sa.Column("per_contract_max_risk", sa.Numeric(18, 2), nullable=True),
    sa.Column("capital_used", sa.Numeric(18, 2), nullable=True),
    sa.Column("max_risk_used", sa.Numeric(18, 2), nullable=True),
    sa.Column("configuration_version", sa.String(length=48), nullable=True),
    sa.Column("ranking_version", sa.String(length=64), nullable=True),
    sa.Column("rejection_summary", _JSON, nullable=True),
    sa.Column("ranked_candidate_ids", _JSON, nullable=True),
    sa.Column("selection_explanation", sa.Text(), nullable=True),
)

_INDEX = "ix_v4_2_challenger_decision_methodology_version"

# The idempotency constraint.
#
# It reads (event, gate version, observed instant) today. Phase 1 and Phase 2
# share this table and deliberately share the gate version -- Phase 2 changes
# the search space, not the thresholds -- so under the existing constraint only
# ONE of the two phases can hold a row for an event, and the overlap period in
# which both run on the same natural events is impossible. Measured: freezing a
# Phase-2 decision and then running Phase 1 on the same control raises
# IntegrityError today.
#
# It is replaced by a unique index over the same three columns plus
# COALESCE(methodology_version, 'v4.2-shared-candidate-v1'). COALESCE rather
# than a plain fourth column because a NULL methodology means Phase 1 and
# Postgres treats NULLs in a unique index as DISTINCT -- a plain fourth column
# would let two Phase-1 rows for one window both be accepted, silently
# weakening the guarantee Phase 1 has today. With the COALESCE every existing
# row keeps exactly the constraint it was written under, and Phase 2 gains room
# for one row of its own.
_OLD_UNIQUE = "uq_v4_2_challenger_decision_event_version_window"
_NEW_UNIQUE = "uq_v4_2_challenger_decision_event_methodology_window"
_PHASE_1 = "v4.2-shared-candidate-v1"


def upgrade() -> None:
    for column in _DECISION_COLUMNS:
        op.add_column(_DECISION, column)
    for column in _CANDIDATE_COLUMNS:
        op.add_column(_CANDIDATE, column)
    for column in _CONFIG_COLUMNS:
        op.add_column(_CONFIG, column)
    # Separating Phase 1 from Phase 2 is the most common question asked of
    # this table, and the answer must never be a sequential scan that invites
    # someone to skip the filter.
    op.create_index(_INDEX, _DECISION, ["methodology_version"])

    op.create_index(
        _NEW_UNIQUE,
        _DECISION,
        [
            "earnings_calendar_event_id",
            "gate_version",
            "observed_at",
            sa.text(f"coalesce(methodology_version, '{_PHASE_1}')"),
        ],
        unique=True,
    )
    # Dropped only after its replacement exists, so the table is never
    # unguarded: every existing row satisfies the new index by construction
    # (all 15 carry a NULL methodology, which COALESCE maps to the Phase-1
    # string, reproducing the old key exactly).
    op.drop_constraint(_OLD_UNIQUE, _DECISION, type_="unique")


def downgrade() -> None:
    op.create_unique_constraint(
        _OLD_UNIQUE,
        _DECISION,
        ["earnings_calendar_event_id", "gate_version", "observed_at"],
    )
    op.drop_index(_NEW_UNIQUE, table_name=_DECISION)
    # Safe to drop: every column added here is nullable, carries only Phase-2
    # evidence, and no Phase-1 row reads any of them. Dropping them leaves the
    # 15 existing challenger decisions byte-identical to what they were.
    op.drop_index(_INDEX, table_name=_DECISION)
    for column in reversed(_CONFIG_COLUMNS):
        op.drop_column(_CONFIG, column.name)
    for column in reversed(_CANDIDATE_COLUMNS):
        op.drop_column(_CANDIDATE, column.name)
    for column in reversed(_DECISION_COLUMNS):
        op.drop_column(_DECISION, column.name)
