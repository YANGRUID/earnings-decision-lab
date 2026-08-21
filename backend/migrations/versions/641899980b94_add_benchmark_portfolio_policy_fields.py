"""add benchmark_portfolio policy fields

Revision ID: 641899980b94
Revises: 201cc8a16cb0
Create Date: 2026-08-21 12:49:49.439168

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '641899980b94'
down_revision: Union[str, Sequence[str], None] = '201cc8a16cb0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Phase 4.4 sec 0B: the official benchmark's policy becomes real,
    # stored data instead of a hardcoded constant in service code.
    # Added nullable first because benchmark_portfolio already has one
    # real row (seeded by 201cc8a16cb0) and Postgres can't add a NOT
    # NULL column with no default to a populated table in one step --
    # same pattern as b83185a6afdb's anchor/date_source columns. Each new
    # enum type is explicitly created before the column that uses it
    # (create_type=False on the column itself) -- confirmed necessary:
    # a bare add_column with an inline Enum and no prior explicit
    # .create() call does NOT auto-create the Postgres type (unlike
    # op.create_table, which at least attempts to), and fails with
    # "type does not exist".
    risk_profile_enum = sa.Enum('CONSERVATIVE', 'MODERATE', 'AGGRESSIVE', name='risk_profile')
    risk_profile_enum.create(op.get_bind(), checkfirst=True)
    op.add_column(
        'benchmark_portfolio',
        sa.Column(
            'risk_profile',
            sa.Enum('CONSERVATIVE', 'MODERATE', 'AGGRESSIVE', name='risk_profile', create_type=False),
            nullable=True,
        ),
    )

    expiration_mode_enum = sa.Enum('AUTO', name='expiration_mode')
    expiration_mode_enum.create(op.get_bind(), checkfirst=True)
    op.add_column(
        'benchmark_portfolio',
        sa.Column(
            'expiration_mode', sa.Enum('AUTO', name='expiration_mode', create_type=False), nullable=True
        ),
    )

    entry_policy_enum = sa.Enum('PRE_EARNINGS_15_55_ET', name='entry_policy')
    entry_policy_enum.create(op.get_bind(), checkfirst=True)
    op.add_column(
        'benchmark_portfolio',
        sa.Column(
            'entry_policy',
            sa.Enum('PRE_EARNINGS_15_55_ET', name='entry_policy', create_type=False),
            nullable=True,
        ),
    )

    exit_policy_enum = sa.Enum('FIRST_POST_EARNINGS_TRADING_DAY_CLOSE', name='exit_policy')
    exit_policy_enum.create(op.get_bind(), checkfirst=True)
    op.add_column(
        'benchmark_portfolio',
        sa.Column(
            'exit_policy',
            sa.Enum(
                'FIRST_POST_EARNINGS_TRADING_DAY_CLOSE', name='exit_policy', create_type=False
            ),
            nullable=True,
        ),
    )

    op.add_column('benchmark_portfolio', sa.Column('is_active', sa.Boolean(), nullable=True))

    # Real data migration: the existing seed row (Phase 4.3,
    # 'Default Benchmark Portfolio') becomes the one official Forward-Test
    # benchmark, per Phase 4.4 sec 0B's exact spec -- renamed in place
    # rather than creating a second row, so every decision_snapshot
    # already pointing at its id stays correctly linked.
    op.execute(
        """
        UPDATE benchmark_portfolio
        SET name = 'AI Earnings Benchmark',
            risk_profile = 'MODERATE',
            expiration_mode = 'AUTO',
            entry_policy = 'PRE_EARNINGS_15_55_ET',
            exit_policy = 'FIRST_POST_EARNINGS_TRADING_DAY_CLOSE',
            is_active = true
        WHERE name = 'Default Benchmark Portfolio'
        """
    )
    # Any other real row (a custom portfolio an owner created by hand)
    # still gets a real, non-null policy -- the same official defaults,
    # never left null -- rather than silently excluded from this backfill.
    op.execute(
        """
        UPDATE benchmark_portfolio
        SET risk_profile = COALESCE(risk_profile, 'MODERATE'),
            expiration_mode = COALESCE(expiration_mode, 'AUTO'),
            entry_policy = COALESCE(entry_policy, 'PRE_EARNINGS_15_55_ET'),
            exit_policy = COALESCE(exit_policy, 'FIRST_POST_EARNINGS_TRADING_DAY_CLOSE'),
            is_active = COALESCE(is_active, true)
        """
    )

    op.alter_column('benchmark_portfolio', 'risk_profile', nullable=False)
    op.alter_column('benchmark_portfolio', 'expiration_mode', nullable=False)
    op.alter_column('benchmark_portfolio', 'entry_policy', nullable=False)
    op.alter_column('benchmark_portfolio', 'exit_policy', nullable=False)
    op.alter_column('benchmark_portfolio', 'is_active', nullable=False)
    # NOTE: autogenerate also wants to drop 'apscheduler_jobs' (its own
    # SQLAlchemyJobStore bookkeeping table, outside Base.metadata) and
    # ix_document_chunk_embedding_hnsw/ix_document_chunk_text_fts (raw-SQL
    # indexes it can't see as ORM-managed). Deliberately not touched here;
    # same false positives as every prior migration's own note.


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(
        "UPDATE benchmark_portfolio SET name = 'Default Benchmark Portfolio' "
        "WHERE name = 'AI Earnings Benchmark'"
    )
    op.drop_column('benchmark_portfolio', 'is_active')
    op.drop_column('benchmark_portfolio', 'exit_policy')
    op.drop_column('benchmark_portfolio', 'entry_policy')
    op.drop_column('benchmark_portfolio', 'expiration_mode')
    op.drop_column('benchmark_portfolio', 'risk_profile')
    sa.Enum(name='exit_policy').drop(op.get_bind(), checkfirst=True)
    sa.Enum(name='entry_policy').drop(op.get_bind(), checkfirst=True)
    sa.Enum(name='expiration_mode').drop(op.get_bind(), checkfirst=True)
    sa.Enum(name='risk_profile').drop(op.get_bind(), checkfirst=True)
