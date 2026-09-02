"""add durable queue columns to research_preparation_job

Revision ID: 656e38387005
Revises: 69653d8b1473
Create Date: 2026-08-25 18:35:29.585077

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '656e38387005'
down_revision: Union[str, Sequence[str], None] = '69653d8b1473'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Two new real values for the existing native Postgres enum backing
    # ResearchPreparationJob.status -- autogenerate does not detect
    # Postgres ENUM value additions (a known SQLAlchemy/Alembic gap), so
    # these are hand-written, not auto-generated. Safe to run inside
    # this migration's own transaction on PG12+ as long as the new
    # values are never referenced in the same transaction (they aren't).
    #
    # Uppercase, matching every existing value in this enum
    # (RUNNING/COMPLETED/...) -- SQLAlchemy's Enum() column type stores
    # a Python enum member's *name*, not its .value, unless
    # values_callable overrides that (it doesn't here), confirmed live
    # against the real DB before writing this.
    op.execute("ALTER TYPE research_job_status ADD VALUE IF NOT EXISTS 'PENDING'")
    op.execute("ALTER TYPE research_job_status ADD VALUE IF NOT EXISTS 'INTERRUPTED'")

    op.add_column('research_preparation_job', sa.Column('earnings_calendar_event_id', sa.Integer(), nullable=True))
    op.add_column('research_preparation_job', sa.Column('heartbeat_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('research_preparation_job', sa.Column('worker_id', sa.String(length=64), nullable=True))
    op.add_column('research_preparation_job', sa.Column('attempt_count', sa.Integer(), server_default='0', nullable=False))
    op.create_index(op.f('ix_research_preparation_job_earnings_calendar_event_id'), 'research_preparation_job', ['earnings_calendar_event_id'], unique=False)
    op.create_foreign_key(None, 'research_preparation_job', 'earnings_calendar_event', ['earnings_calendar_event_id'], ['id'])


def downgrade() -> None:
    """Downgrade schema.

    Postgres cannot drop a single enum value in place -- reverting the
    'pending'/'interrupted' additions would require rebuilding the
    research_job_status type entirely (create a new type, cast every
    existing row's column across, drop the old type). Deliberately not
    attempted here: this migration's downgrade only reverts the new
    columns, matching this project's existing precedent of leaving
    hand-added enum values as a forward-only change when a real
    rebuild isn't warranted.
    """
    op.drop_constraint(None, 'research_preparation_job', type_='foreignkey')
    op.drop_index(op.f('ix_research_preparation_job_earnings_calendar_event_id'), table_name='research_preparation_job')
    op.drop_column('research_preparation_job', 'attempt_count')
    op.drop_column('research_preparation_job', 'worker_id')
    op.drop_column('research_preparation_job', 'heartbeat_at')
    op.drop_column('research_preparation_job', 'earnings_calendar_event_id')
