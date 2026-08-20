"""Phase 4.2 -- read-only eligibility check for an earnings_calendar_event
row: does this candidate deserve a real AI decision later (Phase 4.3, not
built yet)? Pure computation over already-synced data plus one live check
against the configured options provider.

Deliberately never persists a verdict back onto the row -- this module
only returns EligibilityResult objects. Writing SKIPPED/ANALYZED onto
earnings_calendar_event.status is a Phase 4.3+ concern (only once
something actually acts on the verdict); doing it here would blur this
phase's own scope boundary.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from models.earnings_calendar_event import EarningsCalendarEvent
from models.enums import EarningsCalendarEventStatus
from providers.base import OptionsDataProvider

MIN_MARKET_CAP = Decimal("10000000000")  # $10B, per the Phase 4.2 brief
US_COUNTRY_CODE = "US"


@dataclass(frozen=True)
class EligibilityResult:
    symbol: str
    eligible: bool
    reason: str | None = None


def check_eligibility(
    event: EarningsCalendarEvent, options_provider: OptionsDataProvider | None
) -> EligibilityResult:
    """Eligible only if market_cap >= $10B AND US listed AND an options
    provider is configured AND that provider actually returns a tradable
    (non-empty) option chain for the symbol. Checked in that order,
    short-circuiting on the first failure -- the returned reason always
    names the real, specific rule that failed, never a generic catch-all.
    """
    if event.market_cap is None:
        return EligibilityResult(event.symbol, False, "market cap unknown")
    if event.market_cap < MIN_MARKET_CAP:
        return EligibilityResult(
            event.symbol, False, f"market cap below ${MIN_MARKET_CAP:,.0f}"
        )

    if event.country is None:
        return EligibilityResult(event.symbol, False, "listing country unknown")
    if event.country.upper() != US_COUNTRY_CODE:
        return EligibilityResult(event.symbol, False, f"not US listed (country={event.country})")

    if options_provider is None:
        return EligibilityResult(event.symbol, False, "no options provider configured")

    try:
        expirations = options_provider.list_available_expirations(event.symbol, after=date.today())
    except Exception as exc:
        return EligibilityResult(event.symbol, False, f"options chain lookup failed: {exc}")

    if not expirations:
        return EligibilityResult(event.symbol, False, "no tradable option chain")

    return EligibilityResult(event.symbol, True)


def run_eligibility_scan(
    db: Session, options_provider: OptionsDataProvider | None
) -> list[EligibilityResult]:
    """Checks every UPCOMING earnings_calendar_event row -- the real
    candidate set for the pending-analysis queue the Phase 4.2 brief's
    architecture diagram ends at. Does not generate AI decisions and does
    not write anything back to the database; that's Phase 4.3's job."""
    events = (
        db.query(EarningsCalendarEvent)
        .filter(EarningsCalendarEvent.status == EarningsCalendarEventStatus.UPCOMING)
        .order_by(EarningsCalendarEvent.earnings_date.asc())
        .all()
    )
    return [check_eligibility(event, options_provider) for event in events]
