"""Forward-looking, cross-symbol earnings calendar entry (Phase 4.2) --
NOT the existing retrospective ``earnings_event`` table (see
models/earnings_event.py). That table's grain is (company_id,
fiscal_year, fiscal_quarter), populated by SEC-XBRL backfill for events
that have already happened; this one's grain is (symbol, earnings_date),
populated by Finnhub for events that haven't happened yet, discovered
before this project has necessarily ever ingested a filing for the
company at all. Reusing the ``earnings_event`` name/table for this would
either collide with that class outright or silently repurpose a table
this whole codebase depends on for something structurally different --
see PHASE4_ARCHITECTURE_REVIEW.md sec 1.2 for the full reasoning this
mirrors.

Deliberately no ``company_id`` FK -- symbol/company_name are stored
directly, exactly like decision_snapshot's own precedent, since a
Finnhub-discovered symbol may have no ``company`` row at all yet.
"""

from datetime import date
from decimal import Decimal

from sqlalchemy import Date, Enum, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base
from models.enums import EarningsCalendarEventStatus, EarningsSource, EarningsTiming
from models.mixins import TimestampMixin

NUM = Numeric(18, 6)


class EarningsCalendarEvent(TimestampMixin, Base):
    """Grain: one row per (symbol, earnings_date) -- see this module's
    docstring for why that's the key rather than a fiscal-period one, and
    services/earnings_calendar_sync.py for how a genuine date correction
    (same real event, corrected date) is still matched to this row rather
    than creating a duplicate."""

    __tablename__ = "earnings_calendar_event"
    __table_args__ = (
        UniqueConstraint("symbol", "earnings_date", name="uq_earnings_calendar_event_symbol_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    symbol: Mapped[str] = mapped_column(String(16), index=True)
    company_name: Mapped[str] = mapped_column(String(255))
    logo_url: Mapped[str | None] = mapped_column(String(512))

    earnings_date: Mapped[date] = mapped_column(Date, index=True)
    earnings_time: Mapped[EarningsTiming] = mapped_column(
        Enum(EarningsTiming, name="earnings_timing"), default=EarningsTiming.UNKNOWN
    )

    eps_estimate: Mapped[Decimal | None] = mapped_column(NUM)
    revenue_estimate: Mapped[Decimal | None] = mapped_column(NUM)
    # Wider than NUM (18,6) -- a real mega-cap's market cap in dollars
    # (e.g. a ~$3-4T company) overflows an 18-digit/12-integer-digit field.
    # Cents-level precision is meaningless at this scale, so scale=2 here
    # rather than NUM's 6.
    market_cap: Mapped[Decimal | None] = mapped_column(Numeric(20, 2))

    # Not in the Phase 4.2 brief's literal field list -- added because the
    # eligibility filter's "US listed" rule (services/earnings_eligibility.py)
    # needs a real signal, and this is the same Finnhub profile call the
    # sync already makes for logo_url/market_cap (zero extra API cost),
    # so storing it here avoids a second live call at eligibility-check
    # time. See providers/types.py::FinnhubCompanyProfile.country.
    country: Mapped[str | None] = mapped_column(String(4))

    source: Mapped[EarningsSource] = mapped_column(
        Enum(EarningsSource, name="earnings_source"), default=EarningsSource.FINNHUB
    )
    status: Mapped[EarningsCalendarEventStatus] = mapped_column(
        Enum(EarningsCalendarEventStatus, name="earnings_calendar_event_status"),
        default=EarningsCalendarEventStatus.UPCOMING,
        index=True,
    )

    def __repr__(self) -> str:
        return (
            f"EarningsCalendarEvent(symbol={self.symbol!r}, earnings_date={self.earnings_date!r})"
        )
