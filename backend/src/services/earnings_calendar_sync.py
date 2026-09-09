"""Fetches the forward-looking, cross-symbol earnings calendar (EarningsAPI.
com primary, Finnhub fallback -- see providers/factory.py::
build_earnings_calendar_provider and EARNINGS_CALENDAR_PROVIDER_
ARCHITECTURE_REVIEW.md) and upserts it into earnings_calendar_event. The
one real entry point both the daily scheduler job (services/scheduler.py)
and the admin manual-trigger endpoint (api/routers/admin.py) call.

Never deletes: an event that stops appearing in a later sync (already
reported, or now outside the forward window) is left exactly as it was.
This module marks a row COMPLETED once its earnings_date has passed
(_mark_stale_events below); an eligibility scan or real decision-
generation run is what moves a row to ANALYZED/SKIPPED, never this one.
"""

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from models.earnings_calendar_event import EarningsCalendarEvent
from models.enums import EarningsCalendarEventStatus, EarningsSource, EarningsTiming
from providers.base import EarningsCalendarProvider
from providers.types import FinnhubCalendarEntry, FinnhubCompanyProfile

log = logging.getLogger("services.earnings_calendar_sync")

# For earnings-options trading the useful window is ~7-14 days before an
# event (see EARNINGS_CALENDAR_PROVIDER_ARCHITECTURE_REVIEW.md) -- a full
# year of forward calendar was never actually needed, and kept the old
# Finnhub-only sync fetching far-future placeholder dates that don't
# reflect real, currently-scheduled events. 14 also matches
# EarningsApiCalendarProvider's own free-tier rate budget: paired with
# _dates_needing_fetch's per-date dedup below, this keeps real daily
# usage to roughly 1-3 requests in steady state.
SYNC_HORIZON_DAYS = 14

#: Dates within this many days of today are ALWAYS re-fetched, even when they
#: already hold rows.
#:
#: Proven defect (2026-09-09). _ranges_needing_fetch treats any date holding at
#: least one row as permanently covered, so a date was fetched exactly once --
#: when it first entered the horizon with nothing on it -- and never again.
#: An earnings date correction is invisible to that design from BOTH ends: the
#: event's arrival on its new date lands on a date already "covered", and its
#: disappearance from the old date is never observed either. ORCL moved from
#: 2026-09-08 to 2026-09-10 at the provider; our Sep-10 rows dated from
#: 2026-08-22, so the correction was unreachable and a full forward lifecycle
#: was generated against a date on which no earnings occurred.
#:
#: Seven days is deliberate: it covers the decision window (T-1) and the
#: research-preparation lead time with room to spare, while costing at most
#: seven extra provider calls per run against EarningsAPI's date-scoped
#: endpoint -- affordable on the free tier, unlike revalidating the full
#: horizon daily.
NEAR_EVENT_REVALIDATION_DAYS = 7

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
    stale_marked: int = 0
    #: UPCOMING rows on a REFETCHED date that the provider no longer lists.
    #: Their date is no longer corroborated by the source that supplied it.
    vanished: list[str] = field(default_factory=list)
    dates_fetched: int = 0
    dates_skipped: int = 0
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

    # SKIPPED is included deliberately: it is the state
    # _reconcile_vanished_events puts a row in when the provider stopped
    # listing it, which is exactly the row a corrected date should adopt.
    # Excluding it (the pre-2026-09-09 behaviour) meant a vanished event could
    # never be reunited with its correction, and a duplicate was created.
    upcoming = (
        db.query(EarningsCalendarEvent)
        .filter(
            EarningsCalendarEvent.symbol == entry.symbol,
            EarningsCalendarEvent.status.in_(
                (
                    EarningsCalendarEventStatus.UPCOMING,
                    EarningsCalendarEventStatus.SKIPPED,
                )
            ),
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


def _ranges_needing_fetch(
    db: Session, window_start: date, window_end: date, today: date | None = None
) -> tuple[tuple[date, date], ...]:
    """The minimal set of contiguous ``(start, end)`` date ranges in
    ``[window_start, window_end]`` NOT already covered by at least one
    earnings_calendar_event row (any source) -- consecutive missing days
    are merged into one range so a single provider call still handles
    them, exactly like the original "one range call" design. This is the
    real rate-budget mechanism, not just an optimization:
    EarningsApiCalendarProvider has no range endpoint (see its own module
    docstring) -- every date within a range this function returns
    becomes one real HTTP call underneath regardless of how the range is
    grouped here, but grouping still matters for Finnhub (the fallback),
    whose own get_earnings_calendar is one real call per range however
    wide. In steady state (a daily run against an already-populated
    rolling window) there is exactly one missing day -- the new day
    entering the window -- so this returns exactly one single-day range.

    A date with genuinely zero real events (e.g. most weekends) has no
    row and is therefore re-fetched on each subsequent run until it ages
    out of the window -- a small, self-limiting inefficiency (at most
    SYNC_HORIZON_DAYS re-fetches per empty date), not a correctness gap:
    a 0-event day is indistinguishable from a not-yet-fetched one without
    a separate tracking table, and this project deliberately chose not to
    add one for this (see EARNINGS_CALENDAR_PROVIDER_ARCHITECTURE_REVIEW.
    md's rate-budget section)."""
    covered = {
        row[0]
        for row in db.query(EarningsCalendarEvent.earnings_date)
        .filter(
            EarningsCalendarEvent.earnings_date >= window_start,
            EarningsCalendarEvent.earnings_date <= window_end,
        )
        .distinct()
        .all()
    }
    # A date close to now is never treated as covered: it is exactly where a
    # provider correction still matters and where "fetched once, ever" caused
    # the 2026-09-09 incident. Revalidation is bounded to
    # NEAR_EVENT_REVALIDATION_DAYS so the rate budget stays predictable.
    if today is not None:
        horizon = today + timedelta(days=NEAR_EVENT_REVALIDATION_DAYS)
        covered = {d for d in covered if d < today or d > horizon}
    ranges: list[tuple[date, date]] = []
    range_start: date | None = None
    day = window_start
    while day <= window_end:
        missing = day not in covered
        if missing and range_start is None:
            range_start = day
        elif not missing and range_start is not None:
            ranges.append((range_start, day - timedelta(days=1)))
            range_start = None
        day += timedelta(days=1)
    if range_start is not None:
        ranges.append((range_start, window_end))
    return tuple(ranges)


def _reconcile_vanished_events(
    db: Session,
    entries: list[FinnhubCalendarEntry],
    fetched_ranges: tuple[tuple[date, date], ...],
    today: date,
) -> list[str]:
    """Events we still hold on a date the provider was just asked about, and
    did not return.

    The other half of a date correction, and the half that has no natural
    trigger. When ORCL moved from 2026-09-08 to 2026-09-10 it did not merely
    appear somewhere new -- it also STOPPED appearing on 2026-09-08. Nothing
    observed that, so a stale row stayed authoritative and produced a full
    forward lifecycle spanning no earnings at all.

    Only dates actually re-fetched in this run are judged: a date nobody asked
    about proves nothing about what is on it. A row whose date is no longer
    corroborated is flipped out of UPCOMING so it cannot silently become due,
    and is reported by symbol so an operator can see what moved.

    Deliberately conservative: this never deletes and never guesses a new date.
    If the provider later returns the symbol on its corrected date, the normal
    correction path in _find_existing_row adopts it.
    """
    refetched: set[date] = set()
    for start, end in fetched_ranges:
        day = start
        while day <= end:
            refetched.add(day)
            day += timedelta(days=1)
    if not refetched:
        return []

    returned = {(e.symbol, e.earnings_date) for e in entries}
    vanished: list[str] = []
    rows = (
        db.query(EarningsCalendarEvent)
        .filter(
            EarningsCalendarEvent.status == EarningsCalendarEventStatus.UPCOMING,
            EarningsCalendarEvent.earnings_date.in_(sorted(refetched)),
            EarningsCalendarEvent.earnings_date >= today,
        )
        .all()
    )
    for row in rows:
        if (row.symbol, row.earnings_date) in returned:
            continue
        row.status = EarningsCalendarEventStatus.SKIPPED
        vanished.append(f"{row.symbol}@{row.earnings_date.isoformat()}")
        log.warning(
            "earnings calendar: %s no longer listed on %s by the provider; "
            "the stored date is no longer corroborated and the event will not "
            "enter a decision window on it",
            row.symbol,
            row.earnings_date.isoformat(),
        )
    return vanished


def _mark_stale_events(db: Session, today: date) -> int:
    """UPCOMING rows whose earnings_date has already passed become
    COMPLETED. Only ever touches UPCOMING rows -- a row a real
    eligibility scan or decision-generation run already advanced to
    ANALYZED/SKIPPED keeps that real status regardless of date; this is
    purely "nobody ever looked at this one before it passed," swept
    forward so the dashboard's UPCOMING view stays honest."""
    # A row whose date the provider stopped corroborating is deliberately NOT
    # swept into COMPLETED. Sweeping it there was the second half of the
    # 2026-09-09 trap: the date-correction path in _find_existing_row only
    # matches UPCOMING rows, so once a stale row was auto-completed the
    # correction could never adopt it and a duplicate event was inserted
    # instead. SKIPPED rows stay SKIPPED and are reconcilable.
    stale = (
        db.query(EarningsCalendarEvent)
        .filter(
            EarningsCalendarEvent.status == EarningsCalendarEventStatus.UPCOMING,
            EarningsCalendarEvent.earnings_date < today,
        )
        .all()
    )
    for row in stale:
        row.status = EarningsCalendarEventStatus.COMPLETED
    return len(stale)


def sync_earnings_calendar(
    db: Session,
    provider: EarningsCalendarProvider,
    *,
    today: date | None = None,
    from_date: date | None = None,
) -> EarningsCalendarSyncResult:
    """Fetches events from ``from_date`` (default: ``today``) through
    ``today + SYNC_HORIZON_DAYS`` and upserts each one. ``from_date`` lets
    a caller widen the window backward (e.g. an on-demand admin sync
    covering "since the start of this year", not just forward-looking --
    see api/routers/admin.py) without changing the daily scheduled job's
    own behavior at all, since that job never passes it (stays exactly
    "today forward ``SYNC_HORIZON_DAYS`` days"). The end of the window is
    never widened by ``from_date``.

    Only dates in the window not already covered by an existing
    earnings_calendar_event row are actually fetched from the provider
    (see _ranges_needing_fetch) -- this is the real rate-budget mechanism
    for EarningsAPI.com's free tier, not just an optimization. Each
    unique symbol's company profile (logo_url/market_cap/country) is
    fetched at most once per run -- a profile fetch failure is logged and
    skipped for that symbol only (its calendar row is still
    created/updated from the calendar entry alone), never aborts the run,
    matching this project's established per-item error isolation (e.g.
    services/decision_engine.py's per-step failure containment). Once
    fetching is done, any UPCOMING row whose earnings_date has already
    passed is marked COMPLETED (see _mark_stale_events).
    """
    today = today or date.today()
    window_start = from_date or today
    window_end = today + timedelta(days=SYNC_HORIZON_DAYS)
    result = EarningsCalendarSyncResult()

    ranges_to_fetch = _ranges_needing_fetch(db, window_start, window_end, today=today)
    total_days = (window_end - window_start).days + 1
    result.dates_fetched = sum((end - start).days + 1 for start, end in ranges_to_fetch)
    result.dates_skipped = total_days - result.dates_fetched

    entries: list[FinnhubCalendarEntry] = []
    for range_start, range_end in ranges_to_fetch:
        entries.extend(provider.get_earnings_calendar(range_start, range_end))
    result.fetched = len(entries)

    profile_cache: dict[str, FinnhubCompanyProfile | None] = {}

    for entry in entries:
        if entry.symbol not in profile_cache:
            try:
                profile_cache[entry.symbol] = provider.get_company_profile(entry.symbol)
            except Exception:
                log.warning(
                    "earnings calendar profile fetch failed for %s", entry.symbol, exc_info=True
                )
                result.profile_fetch_failures.append(entry.symbol)
                profile_cache[entry.symbol] = None
        profile = profile_cache[entry.symbol]

        timing = _map_timing(entry.session)
        market_cap = _market_cap_dollars(profile)
        company_name = profile.name if profile and profile.name else entry.symbol
        # entry.source_provider is always exactly "earningsapi" or
        # "finnhub" (see both adapters' own source_provider= literal) --
        # matches EarningsSource's .value 1:1 by construction, never
        # guessed at.
        entry_source = EarningsSource(entry.source_provider)

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
                    source=entry_source,
                )
            )
            result.created += 1
            continue

        changed = False
        if existing.source != entry_source:
            existing.source = entry_source
            changed = True
        if is_date_correction and existing.earnings_date != entry.earnings_date:
            existing.earnings_date = entry.earnings_date
            # A corrected event is live again: it was only SKIPPED because its
            # previous date stopped being corroborated.
            existing.status = EarningsCalendarEventStatus.UPCOMING
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

    result.vanished = _reconcile_vanished_events(db, entries, ranges_to_fetch, today)
    result.stale_marked = _mark_stale_events(db, today)

    log.info(
        "earnings calendar sync: dates_fetched=%d dates_skipped=%d fetched=%d created=%d "
        "updated=%d unchanged=%d date_corrected=%d stale_marked=%d profile_failures=%d",
        result.dates_fetched,
        result.dates_skipped,
        result.fetched,
        result.created,
        result.updated,
        result.unchanged,
        result.date_corrected,
        result.stale_marked,
        len(result.profile_fetch_failures),
    )
    return result
