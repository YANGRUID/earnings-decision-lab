"""widen decision_snapshot for phase 4.3 freezing

Revision ID: 201cc8a16cb0
Revises: 27df376fc38c
Create Date: 2026-08-21 00:31:02.377288

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '201cc8a16cb0'
down_revision: Union[str, Sequence[str], None] = '27df376fc38c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # NOTE: autogenerate also wants to drop 'apscheduler_jobs' -- that
    # table is APScheduler's own SQLAlchemyJobStore bookkeeping table
    # (services/scheduler.py), created outside Base.metadata, so
    # autogenerate sees it as "in the DB but not in the ORM models" and
    # proposes dropping it. Doing so would break the Phase 4.2 scheduler's
    # persisted job registration. Deliberately not touched here -- same
    # category of false positive as the document_chunk index note below.
    op.add_column('decision_snapshot', sa.Column('earnings_calendar_event_id', sa.Integer(), nullable=False))
    op.add_column('decision_snapshot', sa.Column('benchmark_portfolio_id', sa.Integer(), nullable=False))
    op.add_column('decision_snapshot', sa.Column('underlying_price', sa.Numeric(precision=18, scale=6), nullable=True))
    op.add_column('decision_snapshot', sa.Column('implied_volatility', sa.Numeric(precision=18, scale=6), nullable=True))
    op.add_column('decision_snapshot', sa.Column('volatility_regime', sa.String(length=16), nullable=True))
    op.add_column('decision_snapshot', sa.Column('option_snapshot_reference', sa.Integer(), nullable=True))
    op.add_column('decision_snapshot', sa.Column('strategy_score', sa.Integer(), nullable=True))
    op.add_column('decision_snapshot', sa.Column('score_breakdown', sa.JSON(), nullable=True))
    op.add_column('decision_snapshot', sa.Column('selected_expiration', sa.Date(), nullable=True))
    op.add_column('decision_snapshot', sa.Column('legs', sa.JSON(), nullable=True))
    op.add_column('decision_snapshot', sa.Column('estimated_probability', sa.Numeric(precision=18, scale=6), nullable=True))
    op.add_column('decision_snapshot', sa.Column('confidence_interval', sa.JSON(), nullable=True))
    op.add_column('decision_snapshot', sa.Column('historical_sample_size', sa.Integer(), nullable=True))
    op.add_column('decision_snapshot', sa.Column('historical_compatibility', sa.JSON(), nullable=True))
    op.add_column('decision_snapshot', sa.Column('why_this_strategy', sa.JSON(), nullable=True))
    op.add_column('decision_snapshot', sa.Column('why_this_expiration', sa.JSON(), nullable=True))
    op.add_column('decision_snapshot', sa.Column('why_these_strikes', sa.JSON(), nullable=True))
    op.add_column('decision_snapshot', sa.Column('why_not_alternatives', sa.JSON(), nullable=True))
    op.add_column('decision_snapshot', sa.Column('engine_version', sa.String(length=32), nullable=False))
    op.add_column('decision_snapshot', sa.Column('prompt_version', sa.String(length=32), nullable=False))
    op.add_column('decision_snapshot', sa.Column('expiration_source', sa.String(length=32), nullable=False))
    op.alter_column('decision_snapshot', 'strategy_type',
               existing_type=sa.VARCHAR(length=32),
               nullable=True)
    op.create_index(op.f('ix_decision_snapshot_benchmark_portfolio_id'), 'decision_snapshot', ['benchmark_portfolio_id'], unique=False)
    op.create_index(op.f('ix_decision_snapshot_earnings_calendar_event_id'), 'decision_snapshot', ['earnings_calendar_event_id'], unique=False)
    op.create_index(op.f('ix_decision_snapshot_option_snapshot_reference'), 'decision_snapshot', ['option_snapshot_reference'], unique=False)
    op.create_unique_constraint('uq_decision_snapshot_event_portfolio', 'decision_snapshot', ['earnings_calendar_event_id', 'benchmark_portfolio_id'])
    # Named explicitly (autogenerate defaults to None/auto-named here) so
    # downgrade() can drop each one by name -- an auto-generated name
    # isn't known until after this runs, which would leave downgrade()
    # unable to target them.
    op.create_foreign_key(
        'fk_decision_snapshot_option_snapshot_reference',
        'decision_snapshot', 'volatility_snapshot', ['option_snapshot_reference'], ['id'],
    )
    op.create_foreign_key(
        'fk_decision_snapshot_benchmark_portfolio_id',
        'decision_snapshot', 'benchmark_portfolio', ['benchmark_portfolio_id'], ['id'],
    )
    op.create_foreign_key(
        'fk_decision_snapshot_earnings_calendar_event_id',
        'decision_snapshot', 'earnings_calendar_event', ['earnings_calendar_event_id'], ['id'],
    )
    op.drop_column('decision_snapshot', 'updated_at')
    # NOTE: autogenerate also wants to drop ix_document_chunk_embedding_hnsw
    # and ix_document_chunk_text_fts every time -- both are raw-SQL indexes
    # autogenerate can't see as ORM-managed. Deliberately not touched here;
    # see prior migrations' same note.

    # Phase 4.3 decision #5 (immutability): reuses entry_snapshot's
    # existing reject_snapshot_update() function (created by 78ee400f83ab)
    # rather than defining a new one -- decision_snapshot's design changed
    # from Phase 4.1's "status may still change" to "no UPDATE of frozen
    # decisions, full stop," so the exact same unconditional trigger
    # already used on entry_snapshot/settlement_snapshot fits directly.
    op.execute(
        """
        CREATE TRIGGER decision_snapshot_no_update
        BEFORE UPDATE ON decision_snapshot
        FOR EACH ROW EXECUTE FUNCTION reject_snapshot_update();
        """
    )

    # Seed the one default Benchmark Portfolio row decision_snapshot's new
    # NOT NULL benchmark_portfolio_id FK needs to reference -- this phase's
    # migration scope doesn't add a risk_profile column to
    # benchmark_portfolio (not listed in the Phase 4.3 brief), so
    # services/decision_snapshot_freezing.py uses a hardcoded default risk
    # profile (Moderate) rather than reading one from this row; see that
    # module's own docstring.
    op.execute(
        """
        INSERT INTO benchmark_portfolio (name, initial_capital, cash_balance, created_at, updated_at)
        VALUES ('Default Benchmark Portfolio', 2000.00, 2000.00, now(), now())
        """
    )
    # ### end Alembic commands ###


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(
        "DELETE FROM benchmark_portfolio WHERE name = 'Default Benchmark Portfolio'"
    )
    op.execute("DROP TRIGGER IF EXISTS decision_snapshot_no_update ON decision_snapshot")
    op.add_column(
        'decision_snapshot',
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
    )
    op.drop_constraint(
        'fk_decision_snapshot_earnings_calendar_event_id', 'decision_snapshot', type_='foreignkey'
    )
    op.drop_constraint(
        'fk_decision_snapshot_benchmark_portfolio_id', 'decision_snapshot', type_='foreignkey'
    )
    op.drop_constraint(
        'fk_decision_snapshot_option_snapshot_reference', 'decision_snapshot', type_='foreignkey'
    )
    op.drop_constraint('uq_decision_snapshot_event_portfolio', 'decision_snapshot', type_='unique')
    op.drop_index(op.f('ix_decision_snapshot_option_snapshot_reference'), table_name='decision_snapshot')
    op.drop_index(op.f('ix_decision_snapshot_earnings_calendar_event_id'), table_name='decision_snapshot')
    op.drop_index(op.f('ix_decision_snapshot_benchmark_portfolio_id'), table_name='decision_snapshot')
    op.alter_column('decision_snapshot', 'strategy_type',
               existing_type=sa.VARCHAR(length=32),
               nullable=False)
    op.drop_column('decision_snapshot', 'expiration_source')
    op.drop_column('decision_snapshot', 'prompt_version')
    op.drop_column('decision_snapshot', 'engine_version')
    op.drop_column('decision_snapshot', 'why_not_alternatives')
    op.drop_column('decision_snapshot', 'why_these_strikes')
    op.drop_column('decision_snapshot', 'why_this_expiration')
    op.drop_column('decision_snapshot', 'why_this_strategy')
    op.drop_column('decision_snapshot', 'historical_compatibility')
    op.drop_column('decision_snapshot', 'historical_sample_size')
    op.drop_column('decision_snapshot', 'confidence_interval')
    op.drop_column('decision_snapshot', 'estimated_probability')
    op.drop_column('decision_snapshot', 'legs')
    op.drop_column('decision_snapshot', 'selected_expiration')
    op.drop_column('decision_snapshot', 'score_breakdown')
    op.drop_column('decision_snapshot', 'strategy_score')
    op.drop_column('decision_snapshot', 'option_snapshot_reference')
    op.drop_column('decision_snapshot', 'volatility_regime')
    op.drop_column('decision_snapshot', 'implied_volatility')
    op.drop_column('decision_snapshot', 'underlying_price')
    op.drop_column('decision_snapshot', 'benchmark_portfolio_id')
    op.drop_column('decision_snapshot', 'earnings_calendar_event_id')
