"""V4 settlement timing-policy provenance; retire the V3 scheduler jobs.

Revision ID: e3a5c7d9b1f2
Revises: d2f4b6a81c37
Create Date: 2026-09-02

Additive schema change (two nullable columns) plus one deliberate data
change: the persisted APScheduler rows of the two official V3 jobs
(decision_and_entry_capture, exit_capture) are removed so the job store no
longer refers to job functions the application stops registering. No V3
evidence table is touched here; that is a separate, later migration.
"""

import sqlalchemy as sa
from alembic import op

revision = "e3a5c7d9b1f2"
down_revision = "d2f4b6a81c37"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("v4_shadow_config_settlement") as batch:
        batch.add_column(sa.Column("timing_policy_version", sa.String(64), nullable=True))
    with op.batch_alter_table("v4_shadow_settlement") as batch:
        batch.add_column(sa.Column("timing_policy_version", sa.String(64), nullable=True))
    _delete_retired_jobs(("decision_and_entry_capture", "exit_capture"))


def downgrade() -> None:
    with op.batch_alter_table("v4_shadow_settlement") as batch:
        batch.drop_column("timing_policy_version")
    with op.batch_alter_table("v4_shadow_config_settlement") as batch:
        batch.drop_column("timing_policy_version")


def _delete_retired_jobs(job_ids: tuple[str, ...]) -> None:
    """apscheduler_jobs is created by APScheduler at first start, not by Alembic:
    on a fresh database (CI, a new deployment) there is nothing to retire."""
    bind = op.get_bind()
    if "apscheduler_jobs" not in sa.inspect(bind).get_table_names():
        return
    placeholders = ", ".join(f"'{job_id}'" for job_id in job_ids)
    op.execute(f"DELETE FROM apscheduler_jobs WHERE id IN ({placeholders})")
