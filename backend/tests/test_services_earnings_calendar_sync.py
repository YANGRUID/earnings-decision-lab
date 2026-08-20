"""Phase 4.2 -- unit tests for services/earnings_calendar_sync.py. Uses an
in-memory fake EarningsCalendarProvider, never a live Finnhub call --
matches this project's no-live-network-in-tests policy (see
tests/test_providers_finnhub.py for the provider-layer equivalent, which
mocks httpx instead).
"""

from datetime import UTC, date, datetime
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
