"""add quote acquisition attempt columns and no update trigger

Revision ID: f381a1bde880
Revises: 44917de38fb4
Create Date: 2026-08-26 19:31:54.005596

Phase 4 quote-observability hardening (2026-08-26), Sections 10 and 12:

1. ``provider_error_category`` -- one new nullable column on
   quote_acquisition_attempt, the sanitized exception category (see
   services/entry_failure_taxonomy.py::classify_provider_exception) for a
   row written by the new exception-path telemetry writers (services/
   quote_telemetry.py's persist_entry_exception_telemetry / persist_
   settlement_exception_telemetry). NULL for every existing row and every
   normal (non-exception) poll row going forward.

2. A real Postgres BEFORE UPDATE trigger on quote_acquisition_attempt,
   reusing the exact same reject_snapshot_update() function every other
   Phase 4 evidence table already shares (created by 78ee400f83ab,
   entry_snapshot's own migration) -- strengthens this table's append-
   only rule from "enforced by convention" (its own module docstring, as
   of the prior pass) to "enforced by the database," matching this
   project's existing established pattern for reusing that one shared
   function across tables. Deliberately does NOT add a no-delete trigger:
   nothing in this project's history establishes a delete policy for this
   diagnostic-only table, and Section 12 asks only for UPDATE rejection
   ("if the project intentionally permits diagnostic deletion for
   maintenance, do not introduce an incompatible no-delete policy without
   reason" -- there is no such reason here).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f381a1bde880'
down_revision: Union[str, Sequence[str], None] = '44917de38fb4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'quote_acquisition_attempt',
        sa.Column('provider_error_category', sa.String(length=32), nullable=True),
    )
    op.execute(
        """
        CREATE TRIGGER quote_acquisition_attempt_no_update
        BEFORE UPDATE ON quote_acquisition_attempt
        FOR EACH ROW EXECUTE FUNCTION reject_snapshot_update();
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS quote_acquisition_attempt_no_update ON quote_acquisition_attempt"
    )
    op.drop_column('quote_acquisition_attempt', 'provider_error_category')
