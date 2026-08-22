"""Phase 4.2 -- unit tests for services/earnings_calendar_sync.py. Uses an
in-memory fake EarningsCalendarProvider, never a live Finnhub call --
matches this project's no-live-network-in-tests policy (see
tests/test_providers_finnhub.py for the provider-layer equivalent, which
mocks httpx instead).
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from models.earnings_calendar_event import EarningsCalendarEvent
from models.enums import EarningsCalendarEventStatus, EarningsTiming
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

    def get_earnings_calendar(
        self, from_date: date, to_date: date
    ) -> list[FinnhubCalendarEntry]:
        return self._entries

    def get_company_profile(self, symbol: str) -> FinnhubCompanyProfile | None:
        return self._profiles.get(symbol)


def _entry(
    symbol: str = "TESTNVDA",
    earnings_date: date = date(2026, 11, 25),
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

    result = sync_earnings_calendar(db_session, provider, today=date(2026, 8, 20))
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
    provider = _FakeCalendarProvider([_entry()], {"TESTNVDA": _profile()})
    sync_earnings_calendar(db_session, provider, today=date(2026, 8, 20))
    db_session.flush()

    result2 = sync_earnings_calendar(db_session, provider, today=date(2026, 8, 20))
    db_session.flush()

    assert result2.created == 0
    assert result2.unchanged == 1
    count = db_session.query(EarningsCalendarEvent).filter_by(symbol="TESTNVDA").count()
    assert count == 1


def test_sync_changed_earnings_date_updates_existing_row(db_session):
    provider1 = _FakeCalendarProvider(
        [_entry(earnings_date=date(2026, 11, 25))], {"TESTNVDA": _profile()}
    )
    sync_earnings_calendar(db_session, provider1, today=date(2026, 8, 20))
    db_session.flush()

    provider2 = _FakeCalendarProvider(
        [_entry(earnings_date=date(2026, 11, 26))], {"TESTNVDA": _profile()}
    )
    result2 = sync_earnings_calendar(db_session, provider2, today=date(2026, 8, 20))
    db_session.flush()

    assert result2.created == 0
    assert result2.date_corrected == 1

    rows = db_session.query(EarningsCalendarEvent).filter_by(symbol="TESTNVDA").all()
    assert len(rows) == 1
    assert rows[0].earnings_date == date(2026, 11, 26)


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
    sync_earnings_calendar(db_session, provider, today=date(2026, 8, 20))
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
    result = sync_earnings_calendar(db_session, provider, today=date(2026, 8, 20))
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


def test_sync_defaults_to_today_forward_window_when_from_date_omitted():
    """Phase 4.9 -- the daily scheduled job never passes from_date; this
    must stay byte-for-byte the original Phase 4.2 behavior."""
    provider = _RecordingCalendarProvider([], {})

    sync_earnings_calendar(None, provider, today=date(2026, 8, 20))  # type: ignore[arg-type]

    assert provider.last_window == (date(2026, 8, 20), date(2026, 8, 20) + timedelta(days=365))


def test_sync_from_date_widens_the_window_backward_only():
    """Phase 4.9 -- an on-demand admin sync with from_date=Jan 1 fetches
    from that real past date through today's own forward horizon -- the
    forward end is never widened by from_date (see the function's own
    docstring for why: Finnhub only ever returns real, scheduled events
    for whatever range is asked, so this never risks fabricating a
    future date)."""
    provider = _RecordingCalendarProvider([], {})

    sync_earnings_calendar(
        None,  # type: ignore[arg-type]
        provider,
        today=date(2026, 8, 20),
        from_date=date(2026, 1, 1),
    )

    assert provider.last_window == (date(2026, 1, 1), date(2026, 8, 20) + timedelta(days=365))


def test_sync_from_date_never_fabricates_events_the_provider_does_not_return(db_session):
    """A widened window is only ever a request for more real data --
    sync_earnings_calendar itself never invents rows beyond whatever the
    (fake, in this test; real Finnhub in production) provider actually
    returns for that range."""
    provider = _RecordingCalendarProvider(
        [_entry(symbol="TESTJAN")], {"TESTJAN": _profile(symbol="TESTJAN")}
    )

    result = sync_earnings_calendar(
        db_session, provider, today=date(2026, 8, 20), from_date=date(2026, 1, 1)
    )
    db_session.flush()

    assert result.fetched == 1  # exactly what the fake provider returned, not more
    assert result.created == 1
    row = db_session.query(EarningsCalendarEvent).filter_by(symbol="TESTJAN").one()
    # the real date _entry() defaults to, never fabricated
    assert row.earnings_date == date(2026, 11, 25)
