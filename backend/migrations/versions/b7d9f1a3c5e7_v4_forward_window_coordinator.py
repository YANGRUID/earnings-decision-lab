"""v4.0.0 settlement-priority hardening: forward-window telemetry and the single 15:30 job.

Revision ID: b7d9f1a3c5e7
Revises: f4b6d8e0c2a3
Create Date: 2026-09-03

Adds ``v4_forward_window_telemetry`` (timing evidence for every settlement
attempt and each phase of the 15:30 ET forward window) and retires the two
separate 15:30 registrations (``v4_shadow_decision``, ``v4_shadow_settlement``)
from the persistent APScheduler store: the coordinator job ``v4_forward_window``
replaces them, and a stale row would otherwise fire alongside it. The two
ids live on as the coordinator's recorded phases in ``scheduler_run``.
"""

from alembic import op
import sqlalchemy as sa

revision = "b7d9f1a3c5e7"
down_revision = "f4b6d8e0c2a3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "v4_forward_window_telemetry",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("phase", sa.String(length=16), nullable=False),
        sa.Column(
            "scheduler_run_id", sa.Integer(), sa.ForeignKey("scheduler_run.id"), nullable=True
        ),
        sa.Column(
            "shadow_decision_id",
            sa.Integer(),
            sa.ForeignKey("v4_shadow_decision.id"),
            nullable=True,
        ),
        sa.Column("symbol", sa.String(length=16), nullable=True),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("job_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("due_detected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("market_data_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("market_data_acquired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_contract_request_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("required_side_ready_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lock_wait_ms", sa.Integer(), nullable=True),
        sa.Column("total_ms", sa.Integer(), nullable=True),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index(
        "ix_v4_forward_window_telemetry_phase", "v4_forward_window_telemetry", ["phase"]
    )
    op.create_index(
        "ix_v4_forward_window_telemetry_scheduler_run_id",
        "v4_forward_window_telemetry",
        ["scheduler_run_id"],
    )
    op.create_index(
        "ix_v4_forward_window_telemetry_shadow_decision_id",
        "v4_forward_window_telemetry",
        ["shadow_decision_id"],
    )
    op.create_index(
        "ix_v4_forward_window_telemetry_symbol", "v4_forward_window_telemetry", ["symbol"]
    )
    op.execute(
        "DELETE FROM apscheduler_jobs WHERE id IN ('v4_shadow_decision', 'v4_shadow_settlement')"
    )


def downgrade() -> None:
    op.drop_table("v4_forward_window_telemetry")
    # The retired registrations are not recreated: the scheduler re-registers
    # whatever the running code declares on its next start.
