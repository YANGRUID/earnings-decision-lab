"""Phase 4.2 -- unit tests for services/earnings_calendar_sync.py. Uses an
in-memory fake EarningsCalendarProvider, never a live Finnhub/EarningsAPI
call -- matches this project's no-live-network-in-tests policy (see
tests/test_providers_finnhub.py for the provider-layer equivalent, which
mocks httpx instead).

``today``/``from_date`` values below deliberately use 2029-2030, well
past every real earnings_date already committed to this shared dev
Postgres instance (real syncs against the real providers have populated
real rows spanning 2026-08 through 2027-07 at the time of writing) --
_ranges_needing_fetch's own per-date dedup now queries the DB before
calling the provider, so a test window that overlapped real committed
data would see it too and split into extra, unexpected provider calls.
Using a safely future window keeps these tests deterministic regardless
of how much real data has accumulated.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from models.earnings_calendar_event import EarningsCalendarEvent
from models.enums import EarningsCalendarEventStatus, EarningsSource, EarningsTiming
from providers.base import EarningsCalendarProvider
from providers.types import FinnhubCalendarEntry, FinnhubCompanyProfile
from services.earnings_calendar_sync import sync_earnings_calendar


class _FakeCalendarProvider(EarningsCalendarProvider):
    def __init__(
        self,
        entries: list[FinnhubCalendarEntry],
        profiles: dict[str, FinnhubCompanyProfile],
    ) -> None:
        self._entries = entries
        self._profiles = profiles

    def get_earnings_calendar(self, from_date: date, to_date: date) -> list[FinnhubCalendarEntry]:
        # Filters by the requested range, matching what a real provider
        # would honestly do -- needed now that _ranges_needing_fetch can
        # split a window into multiple range calls (see
        # test_sync_upsert_does_not_duplicate_unchanged_event, which
        # depends on an already-covered date's range call correctly
        # returning nothing).
        return [e for e in self._entries if from_date <= e.earnings_date <= to_date]

    def get_company_profile(self, symbol: str) -> FinnhubCompanyProfile | None:
        return self._profiles.get(symbol)


def _entry(
    symbol: str = "TESTNVDA",
    earnings_date: date = date(2030, 1, 5),  # after every test's `today` below
    session: str = "amc",
    **overrides: object,
) -> FinnhubCalendarEntry:
    defaults: dict[str, object] = dict(
        symbol=symbol,
        earnings_date=earnings_date,
        session=session,
        fiscal_year=2027,
        fiscal_quarter=3,
        eps_estimate=Decimal("1.25"),
        revenue_estimate=Decimal("35000000000"),
        source_provider="finnhub",
        retrieved_at=datetime.now(UTC),
    )
    defaults.update(overrides)
    return FinnhubCalendarEntry(**defaults)  # type: ignore[arg-type]


def _profile(symbol: str = "TESTNVDA", **overrides: object) -> FinnhubCompanyProfile:
    defaults: dict[str, object] = dict(
        symbol=symbol,
        name="Test NVIDIA Corporation",
        logo_url="https://example.com/logo.png",
        exchange="NASDAQ",
        country="US",
        market_cap_millions=Decimal("3200000"),
        currency="USD",
        source_provider="finnhub",
        retrieved_at=datetime.now(UTC),
    )
    defaults.update(overrides)
    return FinnhubCompanyProfile(**defaults)  # type: ignore[arg-type]


def test_sync_parses_finnhub_response_and_creates_event(db_session):
    provider = _FakeCalendarProvider([_entry()], {"TESTNVDA": _profile()})

    result = sync_earnings_calendar(db_session, provider, today=date(2030, 1, 1))
    db_session.flush()

    assert result.fetched == 1
    assert result.created == 1

    row = db_session.query(EarningsCalendarEvent).filter_by(symbol="TESTNVDA").one()
    assert row.company_name == "Test NVIDIA Corporation"
    assert row.logo_url == "https://example.com/logo.png"
    assert row.country == "US"
    assert row.market_cap == Decimal("3200000000000")  # millions -> real dollars
    assert row.earnings_time == EarningsTiming.AMC
    assert row.status == EarningsCalendarEventStatus.UPCOMING


def test_sync_upsert_does_not_duplicate_unchanged_event(db_session):
    """A second sync of the same window is a total no-op, not just a
    non-duplicating one: the per-date dedup (_ranges_needing_fetch) sees
    the event's date is already covered by the row the first sync
    created and never asks the provider about it again -- a stronger
    guarantee than the pre-dedup design, which re-fetched and compared
    every date on every run. There is nothing to mark "unchanged" the
    second time because nothing is fetched at all."""
    provider = _FakeCalendarProvider([_entry()], {"TESTNVDA": _profile()})
    sync_earnings_calendar(db_session, provider, today=date(2030, 1, 1))
    db_session.flush()

    result2 = sync_earnings_calendar(db_session, provider, today=date(2030, 1, 1))
    db_session.flush()

    assert result2.fetched == 0  # the covered date is skipped, never re-requested
    assert result2.created == 0
    assert result2.unchanged == 0
    count = db_session.query(EarningsCalendarEvent).filter_by(symbol="TESTNVDA").count()
    assert count == 1


def test_sync_changed_earnings_date_updates_existing_row(db_session):
    provider1 = _FakeCalendarProvider(
        [_entry(earnings_date=date(2030, 1, 10))], {"TESTNVDA": _profile()}
    )
    sync_earnings_calendar(db_session, provider1, today=date(2030, 1, 1))
    db_session.flush()

    provider2 = _FakeCalendarProvider(
        [_entry(earnings_date=date(2030, 1, 11))], {"TESTNVDA": _profile()}
    )
    result2 = sync_earnings_calendar(db_session, provider2, today=date(2030, 1, 1))
    db_session.flush()

    assert result2.created == 0
    assert result2.date_corrected == 1

    rows = db_session.query(EarningsCalendarEvent).filter_by(symbol="TESTNVDA").all()
    assert len(rows) == 1
    assert rows[0].earnings_date == date(2030, 1, 11)


def test_sync_never_deletes_historical_events(db_session):
    historical = EarningsCalendarEvent(
        symbol="TESTMU",
        company_name="Test Micron Technology",
        earnings_date=date(2025, 9, 23),
        earnings_time=EarningsTiming.AMC,
        status=EarningsCalendarEventStatus.COMPLETED,
    )
    db_session.add(historical)
    db_session.flush()
    historical_id = historical.id

    provider = _FakeCalendarProvider([_entry(symbol="TESTNVDA")], {"TESTNVDA": _profile()})
    sync_earnings_calendar(db_session, provider, today=date(2030, 1, 1))
    db_session.flush()

    db_session.expire_all()
    reloaded = db_session.get(EarningsCalendarEvent, historical_id)
    assert reloaded is not None
    assert reloaded.status == EarningsCalendarEventStatus.COMPLETED


def test_sync_profile_failure_does_not_abort_run(db_session):
    class _FailingProfileProvider(_FakeCalendarProvider):
        def get_company_profile(self, symbol: str) -> FinnhubCompanyProfile | None:
            raise RuntimeError("Finnhub profile endpoint down")

    provider = _FailingProfileProvider([_entry()], {})
    result = sync_earnings_calendar(db_session, provider, today=date(2030, 1, 1))
    db_session.flush()

    assert result.created == 1
    assert result.profile_fetch_failures == ["TESTNVDA"]
    row = db_session.query(EarningsCalendarEvent).filter_by(symbol="TESTNVDA").one()
    assert row.logo_url is None
    assert row.market_cap is None


class _RecordingCalendarProvider(_FakeCalendarProvider):
    """Phase 4.9 -- records the real (from_date, to_date) window it was
    called with, so tests can assert sync_earnings_calendar's own
    from_date widening logic without needing a live Finnhub call."""

    def __init__(self, entries, profiles) -> None:  # noqa: ANN001
        super().__init__(entries, profiles)
        self.last_window: tuple[date, date] | None = None

    def get_earnings_calendar(self, from_date: date, to_date: date) -> list[FinnhubCalendarEntry]:
        self.last_window = (from_date, to_date)
        return self._entries


def test_sync_defaults_to_today_forward_window_when_from_date_omitted(db_session):
    """The daily scheduled job never passes from_date -- the window must
    be exactly [today, today + SYNC_HORIZON_DAYS]. Needs a real (even if
    empty) db_session now: the per-date dedup (_ranges_needing_fetch)
    queries the DB before ever calling the provider, unlike the original
    Phase 4.2 version."""
    provider = _RecordingCalendarProvider([], {})

    sync_earnings_calendar(db_session, provider, today=date(2030, 1, 1))

    assert provider.last_window == (date(2030, 1, 1), date(2030, 1, 1) + timedelta(days=14))


def test_sync_from_date_widens_the_window_backward_only(db_session):
    """An on-demand admin sync with from_date=Jan 1 fetches from that
    real past date through today's own forward horizon -- the forward
    end is never widened by from_date (see the function's own docstring
    for why: the provider only ever returns real, scheduled events for
    whatever range is asked, so this never risks fabricating a future
    date)."""
    provider = _RecordingCalendarProvider([], {})

    sync_earnings_calendar(
        db_session,
        provider,
        today=date(2030, 1, 1),
        from_date=date(2029, 1, 1),
    )

    assert provider.last_window == (date(2029, 1, 1), date(2030, 1, 1) + timedelta(days=14))


def test_sync_from_date_never_fabricates_events_the_provider_does_not_return(db_session):
    """A widened window is only ever a request for more real data --
    sync_earnings_calendar itself never invents rows beyond whatever the
    (fake, in this test; real Finnhub in production) provider actually
    returns for that range."""
    provider = _RecordingCalendarProvider(
        [_entry(symbol="TESTJAN")], {"TESTJAN": _profile(symbol="TESTJAN")}
    )

    result = sync_earnings_calendar(
        db_session, provider, today=date(2030, 1, 1), from_date=date(2029, 1, 1)
    )
    db_session.flush()

    assert result.fetched == 1  # exactly what the fake provider returned, not more
    assert result.created == 1
    row = db_session.query(EarningsCalendarEvent).filter_by(symbol="TESTJAN").one()
    # the real date _entry() defaults to, never fabricated
    assert row.earnings_date == date(2030, 1, 5)


def test_sync_skips_dates_already_covered_by_an_existing_row(db_session):
    """The real rate-budget mechanism: a date that already has ≥1
    earnings_calendar_event row (any source) is never re-requested from
    the provider -- this is what keeps EarningsAPI.com's real per-date
    HTTP calls down to roughly 1-3/day in steady state. Simulates the
    steady-state rolling window directly: every day already covered
    except the one new day entering the window."""
    today = date(2030, 1, 1)
    window_end = today + timedelta(days=14)  # matches SYNC_HORIZON_DAYS
    day = today
    i = 0
    while day < window_end:
        db_session.add(
            EarningsCalendarEvent(
                symbol=f"TCOV{i}",
                company_name="Test Covered Co",
                earnings_date=day,
                earnings_time=EarningsTiming.AMC,
                status=EarningsCalendarEventStatus.UPCOMING,
            )
        )
        day += timedelta(days=1)
        i += 1
    db_session.flush()

    provider = _RecordingCalendarProvider([], {})
    result = sync_earnings_calendar(db_session, provider, today=today)
    db_session.flush()

    assert provider.last_window == (window_end, window_end)
    assert result.dates_fetched == 1
    assert result.dates_skipped == 14


def test_sync_marks_past_upcoming_events_completed(db_session):
    """Step 6.3 -- an UPCOMING row whose earnings_date has already
    passed is swept to COMPLETED once a sync runs, so the dashboard's
    UPCOMING view never shows a stale past date."""
    past_upcoming = EarningsCalendarEvent(
        symbol="TESTPAST",
        company_name="Test Past Co",
        earnings_date=date(2026, 8, 10),
        earnings_time=EarningsTiming.AMC,
        status=EarningsCalendarEventStatus.UPCOMING,
    )
    already_analyzed = EarningsCalendarEvent(
        symbol="TESTANALYZED",
        company_name="Test Analyzed Co",
        earnings_date=date(2026, 8, 10),
        earnings_time=EarningsTiming.AMC,
        status=EarningsCalendarEventStatus.ANALYZED,
    )
    db_session.add_all([past_upcoming, already_analyzed])
    db_session.flush()

    provider = _FakeCalendarProvider([], {})
    result = sync_earnings_calendar(db_session, provider, today=date(2030, 1, 1))
    db_session.flush()

    # _mark_stale_events is a deliberate, unscoped whole-table sweep (any
    # UPCOMING row anywhere with a past earnings_date, not just this
    # test's own two rows) -- this test's transaction can also see real,
    # already-committed UPCOMING rows from real syncs against this shared
    # dev Postgres instance, all dated well before 2030-01-01, so an
    # exact count here isn't meaningful. >= 1 confirms the fixture row
    # was swept; the two per-row assertions below are what actually
    # verifies the real behavior under test.
    assert result.stale_marked >= 1
    db_session.expire_all()
    assert (
        db_session.query(EarningsCalendarEvent).filter_by(symbol="TESTPAST").one().status
        == EarningsCalendarEventStatus.COMPLETED
    )
    # A row a real decision-generation run already advanced keeps its
    # own real status regardless of date -- never overwritten here.
    assert (
        db_session.query(EarningsCalendarEvent).filter_by(symbol="TESTANALYZED").one().status
        == EarningsCalendarEventStatus.ANALYZED
    )


def test_sync_records_which_provider_actually_supplied_each_event(db_session):
    """source= is set explicitly from entry.source_provider on both the
    create and update paths -- never left to the column default, so the
    dashboard/system-status can honestly report which provider (primary
    EarningsAPI.com or fallback Finnhub) supplied each real row."""
    provider = _FakeCalendarProvider(
        [_entry(symbol="TESTAPI", source_provider="earningsapi")],
        {"TESTAPI": _profile(symbol="TESTAPI", source_provider="earningsapi")},
    )
    sync_earnings_calendar(db_session, provider, today=date(2030, 1, 1))
    db_session.flush()

    row = db_session.query(EarningsCalendarEvent).filter_by(symbol="TESTAPI").one()
    assert row.source == EarningsSource.EARNINGSAPI

    # A later run where the same symbol's date moves to a not-yet-covered
    # day, now supplied by the Finnhub fallback, updates source= via the
    # date-correction path (_find_existing_row's "single UPCOMING row for
    # this symbol" match). This is the only real path source= can change
    # on an update under the per-date dedup design: an *unchanged* date
    # is never re-fetched at all (see test_sync_upsert_does_not_
    # duplicate_unchanged_event), so a same-date provider swap can't be
    # observed through a second sync call the way it could before dedup.
    fallback_provider = _FakeCalendarProvider(
        [_entry(symbol="TESTAPI", earnings_date=date(2030, 1, 6), source_provider="finnhub")],
        {"TESTAPI": _profile(symbol="TESTAPI", source_provider="finnhub")},
    )
    result2 = sync_earnings_calendar(db_session, fallback_provider, today=date(2030, 1, 1))
    db_session.flush()

    db_session.expire_all()
    assert result2.date_corrected == 1
    assert result2.updated == 1
    row2 = db_session.query(EarningsCalendarEvent).filter_by(symbol="TESTAPI").one()
    assert row2.source == EarningsSource.FINNHUB
    assert row2.earnings_date == date(2030, 1, 6)
