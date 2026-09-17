"""Fetches the forward-looking, cross-symbol earnings calendar (EarningsAPI.
com primary, Finnhub fallback -- see providers/factory.py::
build_earnings_calendar_provider and EARNINGS_CALENDAR_PROVIDER_
ARCHITECTURE_REVIEW.md) and upserts it into earnings_calendar_event. The
one real entry point both the daily scheduler job (services/scheduler.py)
and the admin manual-trigger endpoint (api/routers/admin.py) call.

Never deletes. This module marks a row COMPLETED once its earnings_date has
passed (_mark_stale_events below), and SKIPPED when the provider that is
authoritative for the row stops listing it on its date
(_reconcile_vanished_events).

Provider authority (2026-09-17). The two providers are not equally reliable,
and the table used to remember only which one last wrote a row. Every rule
below compares providers by providers/factory.py::
earnings_calendar_provider_rank -- the primary outranks the fallback -- against
the provider that last CONFIRMED the row:

* a provider may move a row to another date, change its timing, or declare it
  vanished only if it is at least as authoritative as that confirmation;
* a vanished row is restored only by a provider at least as authoritative as
  the one whose answer omitted it;
* an operator-verified row (``verified_at``) is never changed by provider data
  at all; a disagreement is reported as a conflict.

Measured defect this replaces: on 2026-09-09 EarningsAPI's daily quota was
spent, the sync fell back to Finnhub for the near-event dates, and 65 events
Finnhub simply did not list -- KR, TCOM and LEN.B among them -- were marked
SKIPPED. Research preparation only considers UPCOMING events, so every one of
them reached its decision window unprepared. The next night EarningsAPI
answered and SKIPPED 40 Finnhub-only rows the same way.
"""

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from models.earnings_calendar_event import EarningsCalendarEvent
from models.enums import EarningsCalendarEventStatus, EarningsSource, EarningsTiming
from providers.base import EarningsCalendarProvider
from providers.factory import earnings_calendar_provider_rank
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

#: A company profile (name, market cap, country) younger than this is reused
#: instead of re-requested. Measured defect: every run re-fetched a profile for
#: every listed symbol, ~80 EarningsAPI requests a day, which spent the free
#: plan's 1,000 monthly requests by 2026-09-13.
PROFILE_REFRESH_DAYS = 7

#: At most this many STALE profiles are refreshed per run. A symbol with no
#: profile at all is always fetched; an old one keeps its stored values until
#: its turn comes. Bounds the first run after deploy, when no row carries a
#: refresh timestamp yet, and keeps the burst inside Finnhub's per-minute limit.
PROFILE_REFRESH_BUDGET_PER_RUN = 25

#: A provider listing a symbol on another date may move an existing row only
#: within this distance. Further apart is a different quarter's report.
MAX_DATE_CORRECTION_DAYS = 45

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
    #: UPCOMING rows on a REFETCHED date that a provider at least as
    #: authoritative as their confirmation no longer lists.
    vanished: list[str] = field(default_factory=list)
    #: SKIPPED rows listed again on their own date by a provider at least as
    #: authoritative as the one that omitted them.
    restored: list[str] = field(default_factory=list)
    #: Provider data that was NOT applied because a more authoritative
    #: confirmation or an operator verification says otherwise.
    conflicts: list[str] = field(default_factory=list)
    dates_fetched: int = 0
    dates_skipped: int = 0
    #: Which provider actually answered, by name -> number of dates.
    dates_answered_by: dict[str, int] = field(default_factory=dict)
    profile_fetch_failures: list[str] = field(default_factory=list)
    profiles_fetched: int = 0
    profiles_reused: int = 0
    #: Providers whose plan allowance was spent during the run (name -> code).
    exhausted_providers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class _ProfileFields:
    name: str | None
    logo_url: str | None
    market_cap: Decimal | None
    country: str | None
    fetched: bool


def _authority(row: EarningsCalendarEvent) -> str | None:
    """The provider whose confirmation currently stands for this row. Rows
    written before confirmation provenance existed fall back to ``source``,
    the provider that last wrote them."""
    if row.last_confirmed_by:
        return row.last_confirmed_by
    source = row.source
    return source.value if isinstance(source, EarningsSource) else source


def _may_overrule(provider: str | None, row: EarningsCalendarEvent) -> bool:
    return earnings_calendar_provider_rank(provider) <= earnings_calendar_provider_rank(
        _authority(row)
    )


def _has_forward_evidence(db: Session, row: EarningsCalendarEvent) -> bool:
    """A V4 decision is frozen against this row's date. Such a row is never
    moved: a later correction would silently rewrite the event a frozen
    decision refers to (see the 2026-09-09 ORCL incident)."""
    if row.id is None:
        return False
    from models.v4_shadow import V4ShadowDecision  # noqa: PLC0415 -- avoids a model import cycle

    return (
        db.query(V4ShadowDecision.id).filter_by(earnings_calendar_event_id=row.id).first()
        is not None
    )


def _find_existing_row(
    db: Session, entry: FinnhubCalendarEntry, today: date | None = None
) -> tuple[EarningsCalendarEvent | None, bool]:
    """Returns ``(row, is_date_correction)``.

    An exact ``(symbol, earnings_date)`` match wins first -- the common,
    unchanged-day-to-day case, and the table's own unique constraint.
    Failing that, a single still-future UPCOMING or SKIPPED row for the same
    symbol, within MAX_DATE_CORRECTION_DAYS of the listed date, is treated as
    the same real event with a corrected date (this table has no fiscal-period
    key to match on instead -- see models/earnings_calendar_event.py). More
    than one candidate is genuinely ambiguous -- which one moved? -- and is
    never guessed at: treated as "no match".

    Never a correction target:

    * a row whose date has passed. SKIPPED rows are not swept to COMPLETED, so
      before this rule KR's September row (decided and settled) would have been
      "corrected" onto its December report the first time that was listed;
    * an operator-verified row;
    * a row with a frozen V4 decision.
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
    query = db.query(EarningsCalendarEvent).filter(
        EarningsCalendarEvent.symbol == entry.symbol,
        EarningsCalendarEvent.status.in_(
            (
                EarningsCalendarEventStatus.UPCOMING,
                EarningsCalendarEventStatus.SKIPPED,
            )
        ),
        EarningsCalendarEvent.verified_at.is_(None),
        EarningsCalendarEvent.earnings_date
        >= entry.earnings_date - timedelta(days=MAX_DATE_CORRECTION_DAYS),
        EarningsCalendarEvent.earnings_date
        <= entry.earnings_date + timedelta(days=MAX_DATE_CORRECTION_DAYS),
    )
    if today is not None:
        query = query.filter(EarningsCalendarEvent.earnings_date >= today)
    candidates = [row for row in query.all() if not _has_forward_evidence(db, row)]
    if len(candidates) == 1:
        return candidates[0], True
    return None, False


def _verified_row_near(db: Session, entry: FinnhubCalendarEntry) -> EarningsCalendarEvent | None:
    """An operator-verified row for the same symbol close enough to be the
    same report. A provider listing on a different date is a conflict with
    it, never a second event."""
    return (
        db.query(EarningsCalendarEvent)
        .filter(
            EarningsCalendarEvent.symbol == entry.symbol,
            EarningsCalendarEvent.verified_at.is_not(None),
            EarningsCalendarEvent.earnings_date
            >= entry.earnings_date - timedelta(days=MAX_DATE_CORRECTION_DAYS),
            EarningsCalendarEvent.earnings_date
            <= entry.earnings_date + timedelta(days=MAX_DATE_CORRECTION_DAYS),
        )
        .order_by(EarningsCalendarEvent.verified_at.desc())
        .first()
    )


def _market_cap_dollars(profile: FinnhubCompanyProfile | None) -> Decimal | None:
    """Dollars, or None when the profile is quoted in another currency: a
    market cap in CNY or HKD read as dollars overstated TCOM ~9x, and the $10B
    eligibility floor is a dollar rule."""
    if profile is None or profile.market_cap_millions is None:
        return None
    if profile.currency and profile.currency.upper() != "USD":
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


def _answering_provider(
    provider: EarningsCalendarProvider, entries: list[FinnhubCalendarEntry]
) -> str | None:
    """Which provider actually answered the last calendar request. The chain
    records it; a bare provider is identified by its entries. None when it
    cannot be told -- and an unattributable answer is never used to declare
    anything vanished."""
    actual = getattr(provider, "last_actual_provider", None)
    if actual:
        return str(actual)
    sources = {e.source_provider for e in entries}
    return sources.pop() if len(sources) == 1 else None


def _reconcile_vanished_events(
    db: Session,
    entries: list[FinnhubCalendarEntry],
    answered_by: dict[date, str | None],
    today: date,
    now: datetime,
) -> list[str]:
    """Events we still hold on a date a provider was just asked about, and
    did not return.

    The other half of a date correction, and the half that has no natural
    trigger. When ORCL moved from 2026-09-08 to 2026-09-10 it did not merely
    appear somewhere new -- it also STOPPED appearing on 2026-09-08. Nothing
    observed that, so a stale row stayed authoritative and produced a full
    forward lifecycle spanning no earnings at all.

    Only dates actually re-fetched in this run are judged, and only by the
    provider that answered for that date -- and only when that provider is at
    least as authoritative as the one that confirmed the row. A fallback
    provider's silence about an event the primary listed is not evidence: it
    covers a different universe of companies (live, 2026-09-09: Finnhub did
    not list KR, TCOM or LEN.B, all three real).

    Deliberately conservative: this never deletes and never guesses a new
    date. If the provider later returns the symbol on its corrected date, the
    normal correction path in _find_existing_row adopts it.
    """
    judged = {d: p for d, p in answered_by.items() if p is not None}
    if not judged:
        return []

    returned = {(e.symbol, e.earnings_date) for e in entries}
    vanished: list[str] = []
    rows = (
        db.query(EarningsCalendarEvent)
        .filter(
            EarningsCalendarEvent.status == EarningsCalendarEventStatus.UPCOMING,
            EarningsCalendarEvent.earnings_date.in_(sorted(judged)),
            EarningsCalendarEvent.earnings_date >= today,
            EarningsCalendarEvent.verified_at.is_(None),
        )
        .all()
    )
    for row in rows:
        if (row.symbol, row.earnings_date) in returned:
            continue
        answering = judged[row.earnings_date]
        if not _may_overrule(answering, row):
            continue
        row.status = EarningsCalendarEventStatus.SKIPPED
        row.vanished_by = answering
        row.vanished_at = now
        vanished.append(f"{row.symbol}@{row.earnings_date.isoformat()}")
        log.warning(
            "earnings calendar: %s no longer listed on %s by %s (confirmed by %s); "
            "the stored date is no longer corroborated and the event will not "
            "enter a decision window on it",
            row.symbol,
            row.earnings_date.isoformat(),
            answering,
            _authority(row),
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
    # swept into COMPLETED: SKIPPED records that its date was not
    # corroborated, which stays true after the date passes.
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


def _stored_profile(db: Session, symbol: str, now: datetime) -> _ProfileFields | None:
    """The freshest profile already on record for ``symbol`` if it is younger
    than PROFILE_REFRESH_DAYS, else None."""
    row = (
        db.query(EarningsCalendarEvent)
        .filter(
            EarningsCalendarEvent.symbol == symbol,
            EarningsCalendarEvent.profile_refreshed_at.is_not(None),
            EarningsCalendarEvent.profile_refreshed_at
            >= now - timedelta(days=PROFILE_REFRESH_DAYS),
        )
        .order_by(EarningsCalendarEvent.profile_refreshed_at.desc())
        .first()
    )
    if row is None:
        return None
    return _ProfileFields(row.company_name, row.logo_url, row.market_cap, row.country, False)


def _any_profile_on_record(db: Session, symbol: str) -> _ProfileFields | None:
    """Any previously stored profile, however old -- used when the refresh
    budget for this run is spent, so a known company keeps its known values
    instead of degrading to "market cap unknown"."""
    row = (
        db.query(EarningsCalendarEvent)
        .filter(
            EarningsCalendarEvent.symbol == symbol,
            EarningsCalendarEvent.market_cap.is_not(None),
        )
        .order_by(EarningsCalendarEvent.updated_at.desc())
        .first()
    )
    if row is None:
        return None
    return _ProfileFields(row.company_name, row.logo_url, row.market_cap, row.country, False)


def sync_earnings_calendar(
    db: Session,
    provider: EarningsCalendarProvider,
    *,
    today: date | None = None,
    from_date: date | None = None,
    now: datetime | None = None,
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
    (see _ranges_needing_fetch), plus the near-event revalidation window.
    A company profile is reused while younger than PROFILE_REFRESH_DAYS; a
    profile fetch failure is logged and skipped for that symbol only, never
    aborts the run. Provider authority rules: see the module docstring.
    """
    today = today or date.today()
    now = now or datetime.now(UTC)
    window_start = from_date or today
    window_end = today + timedelta(days=SYNC_HORIZON_DAYS)
    result = EarningsCalendarSyncResult()

    ranges_to_fetch = _ranges_needing_fetch(db, window_start, window_end, today=today)
    total_days = (window_end - window_start).days + 1
    result.dates_fetched = sum((end - start).days + 1 for start, end in ranges_to_fetch)
    result.dates_skipped = total_days - result.dates_fetched

    entries: list[FinnhubCalendarEntry] = []
    answered_by: dict[date, str | None] = {}
    for range_start, range_end in ranges_to_fetch:
        range_entries = provider.get_earnings_calendar(range_start, range_end)
        answering = _answering_provider(provider, range_entries)
        day = range_start
        while day <= range_end:
            answered_by[day] = answering
            day += timedelta(days=1)
        entries.extend(range_entries)
    result.fetched = len(entries)
    for answering in answered_by.values():
        key = answering or "unattributed"
        result.dates_answered_by[key] = result.dates_answered_by.get(key, 0) + 1

    profile_cache: dict[str, _ProfileFields | None] = {}
    refresh_budget = PROFILE_REFRESH_BUDGET_PER_RUN

    for entry in entries:
        if entry.symbol not in profile_cache:
            stored = _stored_profile(db, entry.symbol, now)
            fallback = None if stored is not None else _any_profile_on_record(db, entry.symbol)
            if stored is not None:
                profile_cache[entry.symbol] = stored
                result.profiles_reused += 1
            elif fallback is not None and refresh_budget <= 0:
                profile_cache[entry.symbol] = fallback
                result.profiles_reused += 1
            else:
                if fallback is not None:
                    refresh_budget -= 1
                try:
                    fetched = provider.get_company_profile(entry.symbol)
                except Exception:
                    log.warning(
                        "earnings calendar profile fetch failed for %s",
                        entry.symbol,
                        exc_info=True,
                    )
                    fetched = None
                    result.profile_fetch_failures.append(entry.symbol)
                if fetched is not None:
                    result.profiles_fetched += 1
                    profile_cache[entry.symbol] = _ProfileFields(
                        fetched.name,
                        fetched.logo_url,
                        _market_cap_dollars(fetched),
                        fetched.country,
                        True,
                    )
                else:
                    profile_cache[entry.symbol] = fallback
        profile = profile_cache[entry.symbol]
        _upsert_entry(db, entry, profile, today=today, now=now, result=result)

    result.vanished = _reconcile_vanished_events(db, entries, answered_by, today, now)
    result.stale_marked = _mark_stale_events(db, today)
    result.exhausted_providers = dict(getattr(provider, "exhausted", {}) or {})

    log.info(
        "earnings calendar sync: dates_fetched=%d dates_skipped=%d answered_by=%s fetched=%d "
        "created=%d updated=%d unchanged=%d date_corrected=%d vanished=%d restored=%d "
        "conflicts=%d stale_marked=%d profiles_fetched=%d profiles_reused=%d "
        "profile_failures=%d exhausted=%s",
        result.dates_fetched,
        result.dates_skipped,
        result.dates_answered_by,
        result.fetched,
        result.created,
        result.updated,
        result.unchanged,
        result.date_corrected,
        len(result.vanished),
        len(result.restored),
        len(result.conflicts),
        result.stale_marked,
        result.profiles_fetched,
        result.profiles_reused,
        len(result.profile_fetch_failures),
        result.exhausted_providers,
    )
    for conflict in result.conflicts:
        log.warning("earnings calendar conflict (not applied): %s", conflict)
    return result


def _upsert_entry(
    db: Session,
    entry: FinnhubCalendarEntry,
    profile: _ProfileFields | None,
    *,
    today: date,
    now: datetime,
    result: EarningsCalendarSyncResult,
) -> None:
    timing = _map_timing(entry.session)
    # entry.source_provider is always exactly "earningsapi" or "finnhub" (see
    # both adapters' own source_provider= literal) -- matches EarningsSource's
    # .value 1:1 by construction, never guessed at.
    entry_source = EarningsSource(entry.source_provider)
    provider_name = entry.source_provider
    label = f"{entry.symbol}@{entry.earnings_date.isoformat()}"

    existing, is_date_correction = _find_existing_row(db, entry, today)

    if existing is None:
        verified_row = _verified_row_near(db, entry)
        if verified_row is not None:
            result.conflicts.append(
                f"{label}: listed by {provider_name}; operator-verified date is "
                f"{verified_row.earnings_date.isoformat()}"
            )
            return
        db.add(
            EarningsCalendarEvent(
                symbol=entry.symbol,
                company_name=(profile.name if profile and profile.name else entry.symbol),
                logo_url=profile.logo_url if profile else None,
                earnings_date=entry.earnings_date,
                earnings_time=timing,
                eps_estimate=entry.eps_estimate,
                revenue_estimate=entry.revenue_estimate,
                market_cap=profile.market_cap if profile else None,
                country=profile.country if profile else None,
                status=EarningsCalendarEventStatus.UPCOMING,
                source=entry_source,
                last_confirmed_by=provider_name,
                last_confirmed_at=now,
                profile_refreshed_at=now if profile and profile.fetched else None,
            )
        )
        result.created += 1
        return

    authoritative = _may_overrule(provider_name, existing)
    if is_date_correction and not authoritative:
        result.conflicts.append(
            f"{label}: listed by {provider_name}; {_authority(existing)} confirmed "
            f"{existing.earnings_date.isoformat()}"
        )
        return

    verified = existing.verified_at is not None
    if verified and existing.status == EarningsCalendarEventStatus.SKIPPED:
        result.conflicts.append(
            f"{label}: still listed by {provider_name}; operator verified that no report "
            "takes place on this date"
        )
    changed = False
    if is_date_correction and existing.earnings_date != entry.earnings_date:
        existing.earnings_date = entry.earnings_date
        # A corrected event is live again: it was only SKIPPED because its
        # previous date stopped being corroborated.
        existing.status = EarningsCalendarEventStatus.UPCOMING
        existing.vanished_by = None
        existing.vanished_at = None
        changed = True
    elif (
        not verified
        and existing.status == EarningsCalendarEventStatus.SKIPPED
        and earnings_calendar_provider_rank(provider_name)
        <= earnings_calendar_provider_rank(existing.vanished_by or _authority(existing))
    ):
        existing.status = EarningsCalendarEventStatus.UPCOMING
        existing.vanished_by = None
        existing.vanished_at = None
        result.restored.append(label)
        changed = True

    if existing.earnings_time != timing:
        if authoritative and not verified:
            existing.earnings_time = timing
            changed = True
        else:
            result.conflicts.append(
                f"{label}: {provider_name} reports timing {timing.value}; "
                f"{'operator verified' if verified else _authority(existing)} "
                f"{getattr(existing.earnings_time, 'value', existing.earnings_time)}"
            )

    if authoritative:
        if existing.source != entry_source:
            existing.source = entry_source
            changed = True
        existing.last_confirmed_by = provider_name
        existing.last_confirmed_at = now
    if entry.eps_estimate is not None and existing.eps_estimate != entry.eps_estimate:
        if authoritative or existing.eps_estimate is None:
            existing.eps_estimate = entry.eps_estimate
            changed = True
    if entry.revenue_estimate is not None and existing.revenue_estimate != entry.revenue_estimate:
        if authoritative or existing.revenue_estimate is None:
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
        if profile.market_cap is not None and existing.market_cap != profile.market_cap:
            existing.market_cap = profile.market_cap
            changed = True
        if profile.fetched:
            existing.profile_refreshed_at = now

    if is_date_correction:
        result.date_corrected += 1
    if changed:
        result.updated += 1
    else:
        result.unchanged += 1
