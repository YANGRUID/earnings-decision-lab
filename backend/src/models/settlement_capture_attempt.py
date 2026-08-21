"""Phase 4.5 -- one official benchmark settlement (exit) capture attempt,
spanning every leg of an already-entered decision_snapshot's position.
Mirrors models/entry_capture_attempt.py exactly, on the closing side:

    DecisionSnapshot
        |
        +-- EntryCaptureAttempt exists with status=CAPTURED?
                |
                +-- yes -> ENTERED
                            |
                            +-- SettlementCaptureAttempt exists with
                                status=CAPTURED?
                                    |
                                    +-- yes -> SETTLED
                                    +-- no  -> still ENTERED, exit still
                                                pending or every exit
                                                attempt so far failed

This table is deliberately a *new* table, not a widening of the existing
``settlement_snapshot`` (Phase 4.1 scaffold) -- see PHASE4_5_SETTLEMENT_
ARCHITECTURE_REVIEW.md's 2026-08-21 addendum for the full reasoning.
``settlement_snapshot`` is left completely untouched by this phase: no
ALTER TABLE, no rows ever written to it here. It remains in the schema,
superseded and unused, the same way any Phase 4.1 scaffold that was never
built out further would.

Append-only, immutable, exactly like EntryCaptureAttempt: a Postgres
BEFORE UPDATE trigger (reusing the same reject_snapshot_update()
function every other Phase 4 snapshot table shares) rejects every
UPDATE. A retry after a failed exit capture always INSERTS a new row;
the partial unique index below still guarantees at most one CAPTURED
attempt per (decision_snapshot, benchmark_portfolio) pair.

Also the natural home for the exit-side underlying market context and
the deterministic P&L/return/R-multiple summary -- computed once from
the individual conservative leg fills via analytics/decision/
settlement_math.py, never a second, parallel implementation. Sizing
(quantity/multiplier per leg) is never recomputed here -- it's frozen at
entry and simply carried forward from the linked EntryCaptureAttempt via
``entry_capture_attempt_id``, which is also where ``initial_max_risk``
(the R-multiple denominator) and ``net_entry_cash`` (the return_pct
denominator) are read from, never duplicated onto this row.
"""

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base
from models.enums import CaptureStatus

if TYPE_CHECKING:
    from models.benchmark_portfolio import BenchmarkPortfolio
    from models.decision_snapshot import DecisionSnapshot
    from models.entry_capture_attempt import EntryCaptureAttempt
    from models.exit_snapshot import ExitSnapshot

NUM = Numeric(18, 6)


class SettlementCaptureAttempt(Base):
    """Grain: one row per (decision_snapshot, benchmark_portfolio,
    attempt) -- indexed, deliberately NOT unique on (decision_snapshot_id,
    benchmark_portfolio_id) alone (append-only retries), but a real
    partial unique index enforces at most one status=CAPTURED row per
    that pair -- see __table_args__.
    """

    __tablename__ = "settlement_capture_attempt"
    __table_args__ = (
        # Same DB-level idempotency guarantee as EntryCaptureAttempt --
        # at most one status=CAPTURED settlement per (decision_snapshot,
        # benchmark_portfolio) pair. Name shortened from the
        # entry-capture-attempt precedent to fit Postgres's 63-character
        # identifier limit (the full "settlement_capture_attempt..."
        # form exceeds it).
        Index(
            "uq_settlement_attempt_one_captured_per_decision_portfolio",
            "decision_snapshot_id",
            "benchmark_portfolio_id",
            unique=True,
            postgresql_where=text("status = 'CAPTURED'"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    decision_snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("decision_snapshot.id"), index=True
    )
    benchmark_portfolio_id: Mapped[int] = mapped_column(
        ForeignKey("benchmark_portfolio.id"), index=True
    )
    # Which entry this settlement closes -- the only way to know what to
    # diff exit prices against, and where the frozen sizing/risk figures
    # (contracts, initial_max_risk, net_entry_cash) live. Nullable only
    # for the one defensive, "should never really happen in production"
    # FAILED row this service writes when it's asked to settle a
    # decision that has no real, CAPTURED entry at all -- the scheduler
    # only ever calls this for decisions already ENTERED (see services/
    # decision_lifecycle.py), so this path is a caller-precondition
    # guard, not a normal outcome. Every real settlement attempt (any
    # status, once a real entry exists) always sets this.
    entry_capture_attempt_id: Mapped[int | None] = mapped_column(
        ForeignKey("entry_capture_attempt.id"), index=True
    )

    status: Mapped[CaptureStatus] = mapped_column(
        Enum(CaptureStatus, name="capture_status", create_type=False),
        default=CaptureStatus.PENDING,
    )
    capture_error: Mapped[str | None] = mapped_column(Text)

    # --- Underlying market context at exit, mirrors EntryCaptureAttempt ---
    underlying_price: Mapped[Decimal | None] = mapped_column(NUM)
    underlying_bid: Mapped[Decimal | None] = mapped_column(NUM)
    underlying_ask: Mapped[Decimal | None] = mapped_column(NUM)
    underlying_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    exit_market_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # --- Multi-leg exit proceeds and realized outcome -- from
    # analytics.decision.settlement_math, never a second, inconsistent
    # implementation. -----------------------------------------------
    net_exit_price_per_share: Mapped[Decimal | None] = mapped_column(NUM)
    net_exit_cash: Mapped[Decimal | None] = mapped_column(NUM)
    realized_pnl: Mapped[Decimal | None] = mapped_column(NUM)
    # realized_pnl / EntryCaptureAttempt.net_entry_cash (the signed,
    # already-computed initial premium paid/received) -- None only when
    # that denominator is exactly zero, never silently reinterpreted.
    return_pct: Mapped[Decimal | None] = mapped_column(NUM)
    # realized_pnl / EntryCaptureAttempt.initial_max_risk -- the
    # already-computed, real risk-defined capital unit, never
    # recalculated here.
    r_multiple: Mapped[Decimal | None] = mapped_column(NUM)
    is_win: Mapped[bool | None] = mapped_column(Boolean)

    source_provider: Mapped[str | None] = mapped_column(String(64))
    captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    decision_snapshot: Mapped["DecisionSnapshot"] = relationship()  # noqa: F821
    benchmark_portfolio: Mapped["BenchmarkPortfolio"] = relationship()  # noqa: F821
    entry_capture_attempt: Mapped["EntryCaptureAttempt"] = relationship()  # noqa: F821
    legs: Mapped[list["ExitSnapshot"]] = relationship(  # noqa: F821
        back_populates="settlement_attempt"
    )

    def __repr__(self) -> str:
        return (
            f"SettlementCaptureAttempt(decision_snapshot_id={self.decision_snapshot_id}, "
            f"status={self.status!r}, realized_pnl={self.realized_pnl!r})"
        )
