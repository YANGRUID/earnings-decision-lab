"""provider usage: which credential sent the request

Additive only, and hand-written: ``alembic revision --autogenerate`` has, in
this repository, proposed dropping ``apscheduler_jobs``, the RAG HNSW index and
the FTS index. One nullable column on ``provider_usage_event``; nothing is
dropped, renamed or rewritten, and no existing row changes value.

Why (2026-09-17). ``provider_usage_event`` records one row per real provider
call, and the Operations calendar card reports those rows as the primary
provider's quota usage. The table had no notion of WHICH KEY sent a request,
so the two are only the same thing while the key never changes. When
EarningsAPI's free allowance was spent and a new key was installed the same
day, the card read "126 / 100 today, 910 / 1000 this month" for a key that had
sent about forty requests and been refused none: every one of those counted
requests belonged to the retired key.

``credential_fingerprint`` is a truncated one-way digest of the key in use
(services/secret_store/resolver.py::secret_fingerprint) -- never the key, and
nothing a reader of this table could authenticate with, keeping the promise
made in models/provider_usage_event.py's own docstring that this table stores
no credential material. It is deliberately NOT backfilled: the credential
behind an existing row is genuinely unknown, and a guess would recreate the
same false attribution in the other direction. A null therefore means
"unknown", never "the current key", and services/operations.py reads it that
way -- a refusal recorded before the active key was first seen cannot be
attributed to it.

Revision ID: b7d1f4a92c60
Revises: a4c6e8b0d2f4
"""

import sqlalchemy as sa
from alembic import op

revision = "b7d1f4a92c60"
down_revision = "a4c6e8b0d2f4"
branch_labels = None
depends_on = None

_TABLE = "provider_usage_event"
_COLUMN = "credential_fingerprint"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column(_COLUMN, sa.String(length=16), nullable=True))


def downgrade() -> None:
    # Safe to drop: the column carries no evidence the product reports on, only
    # attribution of usage rows that remain intact without it.
    op.drop_column(_TABLE, _COLUMN)
