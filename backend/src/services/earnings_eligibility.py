"""Business eligibility for an earnings_calendar_event row: does this
candidate deserve research preparation and, later, a V4 decision? Pure
computation over already-synced data plus one live check against the
configured options provider.

Deliberately never persists a verdict back onto the row -- this module only
returns EligibilityResult objects.

"US listed" (v4.0.1) is a LISTING fact, not a domicile fact: the calendar's
``country`` is where the company is domiciled (Lululemon: CA, Medtronic:
IE), while SEC's own exchange list says where the ticker trades. A
US-domiciled company passes on its country; any other company passes when
SEC lists its ticker on a US exchange. Callers inject the lookup (see
services/us_listing.py) so tests never reach the network; without one the
country rule stands, and a failed lookup is reported as *unverified* and
retryable -- never as "not listed".
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from models.earnings_calendar_event import EarningsCalendarEvent
from models.enums import EarningsCalendarEventStatus
from providers.base import OptionsDataProvider

MIN_MARKET_CAP = Decimal("10000000000")  # $10B, per the Phase 4.2 brief
US_COUNTRY_CODE = "US"

UsListingLookup = Callable[[str], str | None]


@dataclass(frozen=True)
class EligibilityResult:
    symbol: str
    eligible: bool
    reason: str | None = None
    # Post-live correction (2026-08-25): real Aug 25 evidence -- WSM's
    # preparation-time options-chain probe hit a genuine, transient IBKR
    # rate limit and was recorded exactly like a permanent hard filter
    # (market cap, non-US listing), even though WSM's own later,
    # independent execution-time check_eligibility call succeeded. True only
    # for branches that represent an operational failure of a lookup itself,
    # never a genuine, data-driven business-rule rejection.
    retryable: bool = False


def check_us_listing(
    event: EarningsCalendarEvent, us_listing: UsListingLookup | None
) -> tuple[bool, str | None, bool]:
    """(listed, reason_if_not, retryable)."""
    country = (event.country or "").upper()
    if country == US_COUNTRY_CODE:
        return True, None, False
    if us_listing is None:
        if not country:
            return False, "listing country unknown", False
        return False, f"not US listed (country={event.country})", False
    try:
        exchange = us_listing(event.symbol)
    except Exception as exc:  # noqa: BLE001 -- SEC lookup boundary
        return False, f"US listing check unavailable: {exc}", True
    if exchange:
        return True, None, False
    label = f"country={event.country}" if country else "listing country unknown"
    return False, f"not US listed ({label}; no SEC-registered US exchange listing)", False


def check_eligibility(
    event: EarningsCalendarEvent,
    options_provider: OptionsDataProvider | None,
    *,
    us_listing: UsListingLookup | None = None,
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
        return EligibilityResult(event.symbol, False, f"market cap below ${MIN_MARKET_CAP:,.0f}")
    listed, why_not, retryable = check_us_listing(event, us_listing)
    if not listed:
        return EligibilityResult(event.symbol, False, why_not, retryable=retryable)
    if options_provider is None:
        return EligibilityResult(event.symbol, False, "no options provider configured")
    try:
        expirations = options_provider.list_available_expirations(event.symbol, after=date.today())
    except Exception as exc:  # noqa: BLE001 -- provider boundary, reported not raised
        return EligibilityResult(
            event.symbol, False, f"options chain lookup failed: {exc}", retryable=True
        )
    if not expirations:
        return EligibilityResult(event.symbol, False, "no tradable option chain")
    return EligibilityResult(event.symbol, True)


def run_eligibility_scan(
    db: Session,
    options_provider: OptionsDataProvider | None,
    *,
    us_listing: UsListingLookup | None = None,
) -> list[EligibilityResult]:
    """Checks every UPCOMING earnings_calendar_event row. Does not write
    anything back to the database."""
    events = (
        db.query(EarningsCalendarEvent)
        .filter(EarningsCalendarEvent.status == EarningsCalendarEventStatus.UPCOMING)
        .order_by(EarningsCalendarEvent.earnings_date.asc())
        .all()
    )
    return [check_eligibility(event, options_provider, us_listing=us_listing) for event in events]
