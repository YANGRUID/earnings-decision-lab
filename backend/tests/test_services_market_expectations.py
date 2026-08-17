from datetime import UTC, date, datetime
from decimal import Decimal

from models.company import Company
from models.earnings_estimate_snapshot import EarningsEstimateSnapshot
from models.enums import RevisionDirection
from providers.base import EarningsEstimatesProvider
from providers.types import EarningsEstimatePeriod, UpcomingEarningsCalendarEntry
from services.market_expectations import (
    collect_next_earnings_estimate,
    get_latest_earnings_estimate,
)

NOW = datetime.now(UTC)


class _StubEstimatesProvider(EarningsEstimatesProvider):
    def __init__(
        self,
        calendar_entry: UpcomingEarningsCalendarEntry | None,
        periods: list[EarningsEstimatePeriod],
    ) -> None:
        self._calendar_entry = calendar_entry
        self._periods = periods

    def get_earnings_estimates(self, ticker: str) -> list[EarningsEstimatePeriod]:
        return self._periods

    def get_next_earnings_date(self, ticker: str) -> UpcomingEarningsCalendarEntry | None:
        return self._calendar_entry


def _seed_company(db_session, ticker: str = "ZZEST1") -> Company:
    company = Company(ticker=ticker, name="ZZ Estimates Test Co", cik="0009999911")
    db_session.add(company)
    db_session.flush()
    return company


def _calendar_entry(period_end: date, report_date: date) -> UpcomingEarningsCalendarEntry:
    return UpcomingEarningsCalendarEntry(
        ticker="ZZEST1",
        fiscal_period_end_date=period_end,
        estimated_report_date=report_date,
        calendar_eps_estimate=Decimal("1.23"),
        source_provider="alpha_vantage",
        retrieved_at=NOW,
    )


def _period(period_end: date, **overrides) -> EarningsEstimatePeriod:
    defaults = dict(
        ticker="ZZEST1",
        fiscal_period_end_date=period_end,
        horizon="fiscal quarter",
        eps_estimate_average=Decimal("1.50"),
        eps_estimate_high=Decimal("2.00"),
        eps_estimate_low=Decimal("1.00"),
        eps_estimate_analyst_count=20,
        eps_estimate_revision_up_30d=10,
        eps_estimate_revision_down_30d=2,
        revenue_estimate_average=Decimal("5000000000"),
        revenue_estimate_high=Decimal("5500000000"),
        revenue_estimate_low=Decimal("4500000000"),
        revenue_estimate_analyst_count=18,
        source_provider="alpha_vantage",
        retrieved_at=NOW,
    )
    defaults.update(overrides)
    return EarningsEstimatePeriod(**defaults)


class TestCollectNextEarningsEstimate:
    def test_returns_none_when_provider_has_nothing_upcoming(self, db_session):
        company = _seed_company(db_session)
        provider = _StubEstimatesProvider(calendar_entry=None, periods=[])
        assert collect_next_earnings_estimate(db_session, provider, company) is None

    def test_persists_matched_period_with_real_values(self, db_session):
        company = _seed_company(db_session)
        period_end = date(2026, 11, 30)
        provider = _StubEstimatesProvider(
            calendar_entry=_calendar_entry(period_end, date(2026, 12, 15)),
            periods=[_period(period_end)],
        )

        row = collect_next_earnings_estimate(db_session, provider, company)

        assert row is not None
        assert row.fiscal_period_end_date == period_end
        assert row.estimated_report_date == date(2026, 12, 15)
        assert row.eps_estimate_average == Decimal("1.50")
        assert row.eps_estimate_analyst_count == 20
        assert row.eps_revision_direction == RevisionDirection.UP
        assert row.revenue_estimate_average == Decimal("5000000000")
        # No prior snapshot exists yet for this period -- revenue trend is
        # only derivable by comparing against our own stored history.
        assert row.revenue_revision_direction == RevisionDirection.UNKNOWN

    def test_persists_real_date_even_without_matching_detailed_estimate(self, db_session):
        company = _seed_company(db_session)
        period_end = date(2026, 11, 30)
        provider = _StubEstimatesProvider(
            calendar_entry=_calendar_entry(period_end, date(2026, 12, 15)),
            periods=[],  # no detailed EARNINGS_ESTIMATES entry for this period
        )

        row = collect_next_earnings_estimate(db_session, provider, company)

        assert row is not None
        assert row.estimated_report_date == date(2026, 12, 15)
        assert row.eps_estimate_average is None
        assert row.horizon == "unknown"

    def test_second_snapshot_derives_real_revenue_revision_direction(self, db_session):
        company = _seed_company(db_session)
        period_end = date(2026, 11, 30)

        provider_1 = _StubEstimatesProvider(
            calendar_entry=_calendar_entry(period_end, date(2026, 12, 15)),
            periods=[_period(period_end, revenue_estimate_average=Decimal("5000000000"))],
        )
        collect_next_earnings_estimate(db_session, provider_1, company)

        provider_2 = _StubEstimatesProvider(
            calendar_entry=_calendar_entry(period_end, date(2026, 12, 15)),
            periods=[_period(period_end, revenue_estimate_average=Decimal("5200000000"))],
        )
        row2 = collect_next_earnings_estimate(db_session, provider_2, company)

        assert row2.revenue_estimate_average == Decimal("5200000000")
        assert row2.revenue_revision_direction == RevisionDirection.UP


class TestGetLatestEarningsEstimate:
    def test_returns_none_when_no_snapshots_exist(self, db_session):
        company = _seed_company(db_session)
        assert get_latest_earnings_estimate(db_session, company.id) is None

    def test_returns_most_recent_by_period_then_timestamp(self, db_session):
        company = _seed_company(db_session)
        db_session.add(
            EarningsEstimateSnapshot(
                company_id=company.id,
                fiscal_period_end_date=date(2026, 8, 31),
                horizon="fiscal year",
                snapshot_timestamp=NOW,
                eps_revision_direction=RevisionDirection.UNKNOWN,
                revenue_revision_direction=RevisionDirection.UNKNOWN,
                source_provider="alpha_vantage",
                retrieved_at=NOW,
            )
        )
        db_session.flush()

        latest = get_latest_earnings_estimate(db_session, company.id)
        assert latest is not None
        assert latest.fiscal_period_end_date == date(2026, 8, 31)
