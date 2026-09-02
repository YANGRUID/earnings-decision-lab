"""add scheduler_run and scheduler_run_event for operations monitor

Revision ID: 69653d8b1473
Revises: 04327b4aec08
Create Date: 2026-08-25 10:48:35.060187

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '69653d8b1473'
down_revision: Union[str, Sequence[str], None] = '04327b4aec08'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Adds scheduler_run / scheduler_run_event only (Operations Monitor).
    # Autogenerate's raw diff also proposed dropping apscheduler_jobs
    # (a real, existing table SQLAlchemyJobStore creates and manages
    # dynamically -- it isn't a declared ORM model, so autogenerate
    # doesn't recognize it and wants to "remove" it) and two
    # document_chunk indexes (an HNSW vector index and a full-text-search
    # GIN index, both created via raw SQL rather than a plain
    # SQLAlchemy Index() -- same blind spot) -- all three are hand-
    # stripped from this migration; none of them are related to this
    # change.
    op.create_table('scheduler_run',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('job_id', sa.String(length=64), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('duration_ms', sa.Integer(), nullable=True),
    sa.Column('items_evaluated', sa.Integer(), nullable=True),
    sa.Column('items_succeeded', sa.Integer(), nullable=True),
    sa.Column('items_failed', sa.Integer(), nullable=True),
    sa.Column('error_summary', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_scheduler_run_job_id'), 'scheduler_run', ['job_id'], unique=False)
    op.create_index(op.f('ix_scheduler_run_started_at'), 'scheduler_run', ['started_at'], unique=False)
    op.create_table('scheduler_run_event',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('scheduler_run_id', sa.Integer(), nullable=False),
    sa.Column('earnings_calendar_event_id', sa.Integer(), nullable=True),
    sa.Column('symbol', sa.String(length=16), nullable=False),
    sa.Column('stage', sa.String(length=32), nullable=False),
    sa.Column('outcome', sa.String(length=32), nullable=False),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['earnings_calendar_event_id'], ['earnings_calendar_event.id'], ),
    sa.ForeignKeyConstraint(['scheduler_run_id'], ['scheduler_run.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_scheduler_run_event_earnings_calendar_event_id'), 'scheduler_run_event', ['earnings_calendar_event_id'], unique=False)
    op.create_index(op.f('ix_scheduler_run_event_occurred_at'), 'scheduler_run_event', ['occurred_at'], unique=False)
    op.create_index(op.f('ix_scheduler_run_event_scheduler_run_id'), 'scheduler_run_event', ['scheduler_run_id'], unique=False)
    op.create_index(op.f('ix_scheduler_run_event_symbol'), 'scheduler_run_event', ['symbol'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_scheduler_run_event_symbol'), table_name='scheduler_run_event')
    op.drop_index(op.f('ix_scheduler_run_event_scheduler_run_id'), table_name='scheduler_run_event')
    op.drop_index(op.f('ix_scheduler_run_event_occurred_at'), table_name='scheduler_run_event')
    op.drop_index(op.f('ix_scheduler_run_event_earnings_calendar_event_id'), table_name='scheduler_run_event')
    op.drop_table('scheduler_run_event')
    op.drop_index(op.f('ix_scheduler_run_started_at'), table_name='scheduler_run')
    op.drop_index(op.f('ix_scheduler_run_job_id'), table_name='scheduler_run')
    op.drop_table('scheduler_run')
    # ### end Alembic commands ###
