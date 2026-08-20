"""Phase 4.2 -- fetches Finnhub's forward-looking, cross-symbol earnings
calendar and upserts it into earnings_calendar_event. The one real entry
point both the daily scheduler job (services/scheduler.py) and any future
manual trigger call; Phase 4.2 itself only adds read endpoints (see
api/routers/earnings_calendar.py), no write endpoint exists yet.

Never deletes: an event that stops appearing in a later sync (already
reported, or now outside Finnhub's forward window) is left exactly as it
was. Only a later phase's real analysis/settlement step ever moves a
row's status away from UPCOMING -- this module never touches ``status``.
"""

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from models.earnings_calendar_event import EarningsCalendarEvent
from models.enums import EarningsCalendarEventStatus, EarningsTiming
from providers.base import EarningsCalendarProvider
from providers.types import FinnhubCalendarEntry, FinnhubCompanyProfile

log = logging.getLogger("services.earnings_calendar_sync")

# "future 12 months" per the Phase 4.2 brief.
SYNC_HORIZON_DAYS = 365

_SESSION_TO_TIMING = {
    "bmo": EarningsTiming.BMO,
    "amc": EarningsTiming.AMC,
    "dmh": EarningsTiming.DMH,
}


def _map_timing(session: str) -> EarningsTiming:
    return _SESSION_TO_TIMING.get(session.strip().lower(), EarningsTiming.UNKNOWN)


@dataclass
class EarningsCalendarSyncResult:
    fetched: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    date_corrected: int = 0
    profile_fetch_failures: list[str] = field(default_factory=list)


def _find_existing_row(
    db: Session, entry: FinnhubCalendarEntry
) -> tuple[EarningsCalendarEvent | None, bool]:
    """Returns ``(row, is_date_correction)``.

    An exact ``(symbol, earnings_date)`` match wins first -- the common,
    unchanged-day-to-day case, and the table's own unique constraint.
    Failing that, a single UPCOMING row for the same symbol is treated as
    the same real event with a corrected date (this table has no
    fiscal-period key to match on instead -- see
    models/earnings_calendar_event.py). More than one UPCOMING row for the
    symbol is genuinely ambiguous -- which one moved? -- and is never
    guessed at: treated as "no match," so a new row is inserted rather
    than risking silently merging two different real events.
    """
    exact = (
        db.query(EarningsCalendarEvent)
        .filter(
            EarningsCalendarEvent.symbol == entry.symbol,
            EarningsCalendarEvent.earnings_date == entry.earnings_date,
        )
        .one_or_none()
    )
    if exact is not None:
        return exact, False

    upcoming = (
        db.query(EarningsCalendarEvent)
        .filter(
            EarningsCalendarEvent.symbol == entry.symbol,
            EarningsCalendarEvent.status == EarningsCalendarEventStatus.UPCOMING,
        )
        .all()
    )
    if len(upcoming) == 1:
        return upcoming[0], True
    return None, False


def _market_cap_dollars(profile: FinnhubCompanyProfile | None) -> Decimal | None:
    if profile is None or profile.market_cap_millions is None:
        return None
    return profile.market_cap_millions * 1_000_000


def sync_earnings_calendar(
    db: Session,
    provider: EarningsCalendarProvider,
    *,
    today: date | None = None,
) -> EarningsCalendarSyncResult:
    """Fetches the next ``SYNC_HORIZON_DAYS`` of events and upserts each
    one. Each unique symbol's company profile (logo_url/market_cap/
    country) is fetched at most once per run -- a profile fetch failure is
    logged and skipped for that symbol only (its calendar row is still
    created/updated from the calendar entry alone), never aborts the run,
    matching this project's established per-item error isolation (e.g.
    services/decision_engine.py's per-step failure containment).
    """
    today = today or date.today()
    result = EarningsCalendarSyncResult()

    entries = provider.get_earnings_calendar(today, today + timedelta(days=SYNC_HORIZON_DAYS))
    result.fetched = len(entries)

    profile_cache: dict[str, FinnhubCompanyProfile | None] = {}

    for entry in entries:
        if entry.symbol not in profile_cache:
            try:
                profile_cache[entry.symbol] = provider.get_company_profile(entry.symbol)
            except Exception:
                log.warning("Finnhub profile fetch failed for %s", entry.symbol, exc_info=True)
                result.profile_fetch_failures.append(entry.symbol)
                profile_cache[entry.symbol] = None
        profile = profile_cache[entry.symbol]

        timing = _map_timing(entry.session)
        market_cap = _market_cap_dollars(profile)
        company_name = profile.name if profile and profile.name else entry.symbol

        existing, is_date_correction = _find_existing_row(db, entry)

        if existing is None:
            db.add(
                EarningsCalendarEvent(
                    symbol=entry.symbol,
                    company_name=company_name,
                    logo_url=profile.logo_url if profile else None,
                    earnings_date=entry.earnings_date,
                    earnings_time=timing,
                    eps_estimate=entry.eps_estimate,
                    revenue_estimate=entry.revenue_estimate,
                    market_cap=market_cap,
                    country=profile.country if profile else None,
                    status=EarningsCalendarEventStatus.UPCOMING,
                )
            )
            result.created += 1
            continue

        changed = False
        if is_date_correction and existing.earnings_date != entry.earnings_date:
            existing.earnings_date = entry.earnings_date
            changed = True
        if existing.earnings_time != timing:
            existing.earnings_time = timing
            changed = True
        if entry.eps_estimate is not None and existing.eps_estimate != entry.eps_estimate:
            existing.eps_estimate = entry.eps_estimate
            changed = True
        if (
            entry.revenue_estimate is not None
            and existing.revenue_estimate != entry.revenue_estimate
        ):
            existing.revenue_estimate = entry.revenue_estimate
            changed = True
        if profile is not None:
            if profile.name and existing.company_name != profile.name:
                existing.company_name = profile.name
                changed = True
            if profile.logo_url and existing.logo_url != profile.logo_url:
                existing.logo_url = profile.logo_url
                changed = True
            if profile.country and existing.country != profile.country:
                existing.country = profile.country
                changed = True
            if market_cap is not None and existing.market_cap != market_cap:
                existing.market_cap = market_cap
                changed = True

        if is_date_correction:
            result.date_corrected += 1
        if changed:
            result.updated += 1
        else:
            result.unchanged += 1

    log.info(
        "earnings calendar sync: fetched=%d created=%d updated=%d unchanged=%d "
        "date_corrected=%d profile_failures=%d",
        result.fetched,
        result.created,
        result.updated,
        result.unchanged,
        result.date_corrected,
        len(result.profile_fetch_failures),
    )
    return result
