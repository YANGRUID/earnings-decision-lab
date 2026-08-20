"""add decision_snapshot table

Revision ID: 2f7efcd294db
Revises: 3863b30a2f33
Create Date: 2026-08-20 21:15:47.113082

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2f7efcd294db'
down_revision: Union[str, Sequence[str], None] = '3863b30a2f33'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Phase 4.1 (database foundation) -- see PHASE4_ARCHITECTURE_REVIEW.md
    # sec 2.2/2.3 and PHASE4_IMPLEMENTATION_PLAN.md Phase 4.1. This table
    # is the immutable core of the forward-testing infrastructure: exactly
    # one writer (Phase 4.3, not yet built) ever inserts a row, never
    # updates one after -- except the `status` rollup, which is expected
    # to change as entry/settlement capture (Phase 4.4/4.5) progresses.
    #
    # `decision_direction` reuses the existing enum type created by
    # e435021adcc4 (ai_decision_version) -- create_type=False so this
    # migration doesn't try to CREATE TYPE a second time.
    # `decision_direction` (strategy_direction below) is added via a
    # separate add_column step after create_table, not inline -- inline
    # reuse of an already-existing enum type inside create_table does not
    # reliably honor create_type=False in this SQLAlchemy/psycopg
    # combination (confirmed: raises DuplicateObject on decision_direction
    # even with create_type=False set on the inline column). add_column
    # is the codebase's own proven-working pattern for enum reuse (see
    # b83185a6afdb).
    op.create_table('decision_snapshot',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('ticker', sa.String(length=16), nullable=False),
    sa.Column('company_name', sa.String(length=255), nullable=False),
    sa.Column('strategy_type', sa.String(length=32), nullable=False),
    sa.Column('ai_thesis_version_id', sa.Integer(), nullable=True),
    sa.Column('generated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column(
        'status',
        sa.Enum('PENDING_ENTRY', 'ENTERED', 'SETTLED', 'VOID', name='decision_snapshot_status'),
        nullable=False,
    ),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['ai_thesis_version_id'], ['ai_thesis_version.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.add_column(
        'decision_snapshot',
        sa.Column(
            'strategy_direction',
            sa.Enum(
                'STRONG_BULLISH', 'BULLISH', 'NEUTRAL', 'BEARISH', 'STRONG_BEARISH',
                name='decision_direction', create_type=False,
            ),
            nullable=False,
        ),
    )
    op.create_index(op.f('ix_decision_snapshot_ai_thesis_version_id'), 'decision_snapshot', ['ai_thesis_version_id'], unique=False)
    # Required index (ticker) per the Phase 4.1 brief.
    op.create_index(op.f('ix_decision_snapshot_ticker'), 'decision_snapshot', ['ticker'], unique=False)
    # Required index (generated_at) per the Phase 4.1 brief.
    op.create_index(op.f('ix_decision_snapshot_generated_at'), 'decision_snapshot', ['generated_at'], unique=False)
    # NOTE: autogenerate also wants to drop ix_document_chunk_embedding_hnsw
    # and ix_document_chunk_text_fts every time -- both are raw-SQL indexes
    # autogenerate can't see as ORM-managed. Deliberately not touched here;
    # see prior migrations' same note.


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_decision_snapshot_generated_at'), table_name='decision_snapshot')
    op.drop_index(op.f('ix_decision_snapshot_ticker'), table_name='decision_snapshot')
    op.drop_index(op.f('ix_decision_snapshot_ai_thesis_version_id'), table_name='decision_snapshot')
    op.drop_table('decision_snapshot')
    # decision_direction is NOT dropped here -- it's owned by
    # e435021adcc4 (ai_decision_version), which still uses it.
    sa.Enum(name='decision_snapshot_status').drop(op.get_bind(), checkfirst=True)
