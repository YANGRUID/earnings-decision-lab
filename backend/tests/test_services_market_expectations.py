from datetime import UTC, date, datetime
from decimal import Decimal

from models.company import Company
from models.earnings_estimate_snapshot import EarningsEstimateSnapshot
from models.enums import RevisionDirection, UpcomingEarningsDateSource
from providers.base import EarningsEstimatesProvider
from providers.types import EarningsEstimatePeriod, UpcomingEarningsCalendarEntry
from services.market_expectations import (
    collect_next_earnings_estimate,
    get_latest_earnings_estimate,
    set_manual_earnings_date,
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

    def test_prefers_fiscal_quarter_over_fiscal_year_at_same_period_end_date(self, db_session):
        """Real bug, caught live: a company's fiscal Q4/year-end period has
        the *same* fiscal_period_end_date for both its "fiscal quarter" and
        "fiscal year" consensus entries (observed for Micron's real
        EARNINGS_ESTIMATES response). Taking whichever the provider lists
        first would silently substitute the annual figure for what this
        project represents as the next quarterly report's consensus.
        """
        company = _seed_company(db_session)
        period_end = date(2026, 8, 31)
        provider = _StubEstimatesProvider(
            calendar_entry=_calendar_entry(period_end, date(2026, 9, 22)),
            periods=[
                _period(period_end, horizon="fiscal year", eps_estimate_average=Decimal("73.39")),
                _period(
                    period_end, horizon="fiscal quarter", eps_estimate_average=Decimal("31.30")
                ),
            ],
        )

        row = collect_next_earnings_estimate(db_session, provider, company)

        assert row is not None
        assert row.horizon == "fiscal quarter"
        assert row.eps_estimate_average == Decimal("31.30")

    def test_falls_back_to_annual_entry_when_no_quarterly_entry_exists_at_that_date(
        self, db_session
    ):
        company = _seed_company(db_session)
        period_end = date(2026, 8, 31)
        provider = _StubEstimatesProvider(
            calendar_entry=_calendar_entry(period_end, date(2026, 9, 22)),
            periods=[
                _period(period_end, horizon="fiscal year", eps_estimate_average=Decimal("73.39"))
            ],
        )

        row = collect_next_earnings_estimate(db_session, provider, company)

        assert row is not None
        assert row.horizon == "fiscal year"
        assert row.eps_estimate_average == Decimal("73.39")

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


class TestSetManualEarningsDate:
    """Regression tests for the owner/admin manual override that unblocks
    Strategy Lab / options collection when Alpha Vantage has no upcoming
    earnings date on record -- see api/routers/research.py's
    set_earnings_date_override and the real bug this replaces (AMD,
    2026-08-18).
    """

    def test_persists_with_manual_provenance_and_null_consensus(self, db_session):
        company = _seed_company(db_session)

        row = set_manual_earnings_date(db_session, company, date(2026, 11, 4))

        assert row.estimated_report_date == date(2026, 11, 4)
        assert row.date_source == UpcomingEarningsDateSource.MANUAL
        # A manual date is not analyst consensus -- every consensus field
        # must stay null, never fabricated to look like real estimates.
        assert row.eps_estimate_average is None
        assert row.revenue_estimate_average is None
        assert row.eps_estimate_analyst_count is None
        assert row.source_provider == "manual"

    def test_defaults_fiscal_period_end_date_to_report_date_when_not_given(self, db_session):
        company = _seed_company(db_session)

        row = set_manual_earnings_date(db_session, company, date(2026, 11, 4))

        assert row.fiscal_period_end_date == date(2026, 11, 4)

    def test_respects_explicit_fiscal_period_end_date(self, db_session):
        company = _seed_company(db_session)

        row = set_manual_earnings_date(
            db_session, company, date(2026, 11, 4), fiscal_period_end_date=date(2026, 9, 27)
        )

        assert row.fiscal_period_end_date == date(2026, 9, 27)
        assert row.estimated_report_date == date(2026, 11, 4)

    def test_never_overwrites_or_relabels_an_existing_row(self, db_session):
        """A manual override always inserts a new snapshot -- it must never
        mutate a prior row's date_source, e.g. silently turning an existing
        alpha_vantage-confirmed row into one that looks manual."""
        company = _seed_company(db_session)
        provider_row = collect_next_earnings_estimate(
            db_session,
            _StubEstimatesProvider(
                calendar_entry=_calendar_entry(date(2026, 8, 31), date(2026, 9, 22)),
                periods=[_period(date(2026, 8, 31))],
            ),
            company,
        )
        assert provider_row is not None

        set_manual_earnings_date(db_session, company, date(2026, 11, 4))

        db_session.refresh(provider_row)
        assert provider_row.date_source == UpcomingEarningsDateSource.ALPHA_VANTAGE
        assert provider_row.estimated_report_date == date(2026, 9, 22)

    def test_latest_earnings_estimate_picks_up_the_manual_row(self, db_session):
        company = _seed_company(db_session)

        set_manual_earnings_date(db_session, company, date(2026, 11, 4))

        latest = get_latest_earnings_estimate(db_session, company.id)
        assert latest is not None
        assert latest.date_source == UpcomingEarningsDateSource.MANUAL
        assert latest.estimated_report_date == date(2026, 11, 4)


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
