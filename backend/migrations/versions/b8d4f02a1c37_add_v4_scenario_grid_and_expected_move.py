"""Persist the per-scenario T+1 grid and the expected-move context.

V4 consolidation (2026-09-02), Sections 25-26. Two additive JSON columns:

* ``v4_shadow_candidate.scenario_grid`` -- every CORE and TAIL STRESS cell
  the candidate was valued on, kept as two separate lists. Until now only
  the aggregates (worst/median/positive fraction) were frozen, so a
  scenario matrix could only have been recomputed -- and a recomputation
  can drift from what was actually ranked.
* ``v4_shadow_decision.expected_move`` -- spot, implied move, +/-1 EM
  boundaries and the historical median move, frozen once at event level.

WRITTEN BY HAND. This project's Alembic autogenerate proposes dropping
runtime-created structures (apscheduler_jobs, document_chunk HNSW/FTS
indexes); writing directly avoids reintroducing that hazard.

Revision ID: b8d4f02a1c37
Revises: a7c3e91f2d04
"""

import sqlalchemy as sa
from alembic import op

revision = "b8d4f02a1c37"
down_revision = "a7c3e91f2d04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("v4_shadow_candidate", sa.Column("scenario_grid", sa.JSON(), nullable=True))
    op.add_column("v4_shadow_decision", sa.Column("expected_move", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("v4_shadow_decision", "expected_move")
    op.drop_column("v4_shadow_candidate", "scenario_grid")
