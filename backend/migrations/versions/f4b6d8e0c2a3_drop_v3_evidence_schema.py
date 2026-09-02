"""V4-only reset: drop the retired V3 evidence schema and its operational history.

Revision ID: f4b6d8e0c2a3
Revises: e3a5c7d9b1f2
Create Date: 2026-09-03

Authorised, deliberate destruction (the V3 code and data are preserved on
the archive/pre-v4-only-reset branch and in a local pg_dump taken before this
migration ran). Hand-audited: only tables owned exclusively by the V3
decision engine / benchmark pipeline / AI Decision Journal / Cross-Company
Replay are dropped, in foreign-key order. Every shared research table
(company, earnings_event, price_reaction, earnings_result, expectation,
estimate and volatility snapshots, options_snapshot, filings, chunks, AI
theses, calendar, provider tables, scheduler tables, apscheduler_jobs, every
v4_shadow_* table) is untouched. The shared trigger function
reject_snapshot_update() is kept: the V4 tables use it.

Operational history: scheduler_run / scheduler_run_event rows of the two
retired V3 jobs are deleted; runs of the platform jobs (calendar sync,
research preparation, IBKR health) and every V4 run are kept.
"""

from alembic import op
import sqlalchemy as sa

revision = "f4b6d8e0c2a3"
down_revision = "e3a5c7d9b1f2"
branch_labels = None
depends_on = None

_V3_TABLES_IN_DROP_ORDER = (
    "exit_snapshot",  # -> settlement_capture_attempt, entry_snapshot, decision_snapshot
    "settlement_snapshot",  # -> decision_snapshot
    "quote_acquisition_attempt",  # -> entry_capture_attempt, settlement_capture_attempt
    "settlement_capture_attempt",  # -> entry_capture_attempt, decision_snapshot, benchmark_portfolio
    "entry_snapshot",  # -> entry_capture_attempt, decision_snapshot
    "entry_capture_attempt",  # -> decision_snapshot, benchmark_portfolio
    "decision_snapshot",  # -> earnings_calendar_event, ai_thesis_version, benchmark_portfolio
    "benchmark_portfolio",
    "portfolio_position_snapshot",  # legacy IBKR web-gateway positions (V3 Exposure tab)
    "ai_decision_version",  # AI Decision Journal (V3 on-demand decisions)
    "strategy_replay",  # Cross-Company Replay artifacts
)

_V3_JOB_IDS = ("decision_and_entry_capture", "exit_capture")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = set(inspector.get_table_names())
    for table in _V3_TABLES_IN_DROP_ORDER:
        if table in existing:
            op.drop_table(table)
    op.execute(
        "DELETE FROM scheduler_run_event WHERE scheduler_run_id IN "
        "(SELECT id FROM scheduler_run WHERE job_id IN ('decision_and_entry_capture', 'exit_capture'))"
    )
    op.execute(
        "DELETE FROM scheduler_run WHERE job_id IN ('decision_and_entry_capture', 'exit_capture')"
    )


def downgrade() -> None:
    raise RuntimeError(
        "The V3 evidence schema is not recreated by this migration; restore the "
        "archive/pre-v4-only-reset branch and the pre-reset pg_dump instead."
    )
