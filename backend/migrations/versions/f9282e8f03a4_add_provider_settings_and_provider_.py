"""add provider settings and provider health event tables

Revision ID: f9282e8f03a4
Revises: b83185a6afdb
Create Date: 2026-08-18 15:56:16.952847

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f9282e8f03a4'
down_revision: Union[str, Sequence[str], None] = 'b83185a6afdb'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # NOTE: autogenerate also wants to drop ix_document_chunk_embedding_hnsw
    # and ix_document_chunk_text_fts every time -- both are raw-SQL indexes
    # autogenerate can't see as ORM-managed. Manually stripped; see prior
    # migrations.
    op.create_table('app_provider_settings',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('price_history_primary', sa.String(length=32), nullable=True),
    sa.Column('price_history_fallback', sa.String(length=32), nullable=True),
    sa.Column('options_primary', sa.String(length=32), nullable=True),
    sa.Column('options_fallback', sa.String(length=32), nullable=True),
    sa.Column('llm_provider', sa.String(length=32), nullable=True),
    sa.Column('llm_model', sa.String(length=128), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('id = 1', name='ck_app_provider_settings_singleton'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('provider_health_event',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('provider', sa.String(length=32), nullable=False),
    sa.Column('domain', sa.String(length=32), nullable=False),
    sa.Column('status', sa.Enum('CONNECTED', 'AUTH_FAILED', 'RATE_LIMITED', 'PREMIUM_REQUIRED', 'GATEWAY_OFFLINE', 'UNAVAILABLE', 'TIMEOUT', name='provider_health_status'), nullable=False),
    sa.Column('detail', sa.String(length=500), nullable=True),
    sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_provider_health_event_domain'), 'provider_health_event', ['domain'], unique=False)
    op.create_index(op.f('ix_provider_health_event_occurred_at'), 'provider_health_event', ['occurred_at'], unique=False)
    op.create_index(op.f('ix_provider_health_event_provider'), 'provider_health_event', ['provider'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_provider_health_event_provider'), table_name='provider_health_event')
    op.drop_index(op.f('ix_provider_health_event_occurred_at'), table_name='provider_health_event')
    op.drop_index(op.f('ix_provider_health_event_domain'), table_name='provider_health_event')
    op.drop_table('provider_health_event')
    sa.Enum(name='provider_health_status').drop(op.get_bind(), checkfirst=True)
    op.drop_table('app_provider_settings')
