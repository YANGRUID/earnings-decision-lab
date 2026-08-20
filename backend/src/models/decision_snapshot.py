"""Point-in-time AI Benchmark Portfolio decision (Phase 4) -- the
immutable core of the forward-testing infrastructure this phase builds.
See PHASE4_ARCHITECTURE_REVIEW.md sec 2.2/2.3 for the full design and
PHASE4_IMPLEMENTATION_PLAN.md Phase 4.3 for how a row here actually gets
written.

Exactly one writer touches the columns below (whatever service generates
and freezes a decision, Phase 4.3), called once per row, never edited
after. ``status`` is the sole exception -- an operational lifecycle
rollup mutated by the entry/settlement capture jobs (Phase 4.4/4.5), not
a research fact; ``generated_at`` itself never changes once set, even
when ``status`` does.

Deliberately denormalized against earnings_calendar_event/company --
neither is a hard FK here (earnings_calendar_event doesn't exist as of
Phase 4.1; company_id was dropped from this phase's scope). ``ticker``/
``company_name`` are stored directly so a later correction to an
upstream table can never silently change what this row is understood to
have said at generation time.

entry_snapshot/settlement_snapshot (their own tables -- see those
modules) hold everything about what actually happened after generation.
This table never gains entry/exit/P&L columns; see the architecture
review's reversed decision on that (2026-08-20).
"""

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Enum, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base
from models.enums import DecisionDirection, DecisionSnapshotStatus
from models.mixins import TimestampMixin

if TYPE_CHECKING:
    from models.ai_thesis_version import AIThesisVersion
    from models.entry_snapshot import EntrySnapshot
    from models.settlement_snapshot import SettlementSnapshot


class DecisionSnapshot(TimestampMixin, Base):
    """Grain: one row per frozen AI Benchmark Portfolio decision."""

    __tablename__ = "decision_snapshot"

    id: Mapped[int] = mapped_column(primary_key=True)

    ticker: Mapped[str] = mapped_column(String(16), index=True)
    company_name: Mapped[str] = mapped_column(String(255))

    strategy_direction: Mapped[DecisionDirection] = mapped_column(
        Enum(DecisionDirection, name="decision_direction")
    )
    # Free-form, like ai_decision_version.recommended_strategy_category --
    # the strategy engine's candidate set is open-ended (butterflies,
    # condors, spreads, ...), not a fixed short enum.
    strategy_type: Mapped[str] = mapped_column(String(32))

    # Nullable, matching ai_decision_version's own earnings_estimate_
    # snapshot_id/volatility_snapshot_id precedent for optional grounding
    # references.
    ai_thesis_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("ai_thesis_version.id"), index=True
    )

    # The real domain moment of generation -- explicitly set by the
    # writer, never server-defaulted (matches every other provider-
    # timestamp column in this codebase, e.g. retrieved_at elsewhere).
    # Immutable: no onupdate, and never touched by the status transitions
    # below -- see tests/test_models.py for a regression test of this.
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    status: Mapped[DecisionSnapshotStatus] = mapped_column(
        Enum(DecisionSnapshotStatus, name="decision_snapshot_status"),
        default=DecisionSnapshotStatus.PENDING_ENTRY,
    )

    ai_thesis_version: Mapped["AIThesisVersion | None"] = relationship()
    entry_snapshots: Mapped[list["EntrySnapshot"]] = relationship(  # noqa: F821
        back_populates="decision_snapshot"
    )
    settlement_snapshots: Mapped[list["SettlementSnapshot"]] = relationship(  # noqa: F821
        back_populates="decision_snapshot"
    )

    def __repr__(self) -> str:
        return f"DecisionSnapshot(ticker={self.ticker!r}, generated_at={self.generated_at!r})"
