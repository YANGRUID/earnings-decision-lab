from datetime import UTC, date, datetime
from decimal import Decimal

from agents.tools.earnings_estimates import EarningsEstimatesArgs, EarningsEstimatesTool
from models.company import Company
from models.earnings_estimate_snapshot import EarningsEstimateSnapshot
from models.enums import RevisionDirection, UpcomingEarningsDateSource

NOW = datetime.now(UTC)


def _snapshot(company, **overrides) -> EarningsEstimateSnapshot:
    defaults = dict(
        company_id=company.id,
        fiscal_period_end_date=date(2026, 6, 30),
        horizon="next_quarter",
        snapshot_timestamp=NOW,
        eps_estimate_average=Decimal("1.25"),
        eps_estimate_high=Decimal("1.40"),
        eps_estimate_low=Decimal("1.10"),
        eps_estimate_analyst_count=18,
        eps_revision_direction=RevisionDirection.UP,
        revenue_estimate_average=Decimal("500000000"),
        revenue_estimate_high=Decimal("520000000"),
        revenue_estimate_low=Decimal("480000000"),
        revenue_estimate_analyst_count=15,
        revenue_revision_direction=RevisionDirection.FLAT,
        date_source=UpcomingEarningsDateSource.ESTIMATED,
        source_provider="test",
        retrieved_at=NOW,
    )
    defaults.update(overrides)
    return EarningsEstimateSnapshot(**defaults)


def test_returns_real_consensus_when_available(db_session):
    company = Company(ticker="ZZEST1", name="ZZ Estimate Test 1", cik="0009991001")
    db_session.add(company)
    db_session.flush()
    db_session.add(_snapshot(company))
    db_session.flush()

    tool = EarningsEstimatesTool(db_session)
    outcome = tool.run(EarningsEstimatesArgs(ticker="ZZEST1"))

    assert outcome.success
    assert outcome.data["available"] is True
    assert outcome.data["eps_estimate_average"] == "1.250000"
    assert outcome.data["eps_estimate_analyst_count"] == 18
    assert outcome.data["eps_revision_direction_30d"] == "up"
    assert outcome.data["revenue_revision_direction_30d"] == "flat"


def test_reports_unavailable_honestly_when_none_collected(db_session):
    company = Company(ticker="ZZEST2", name="ZZ Estimate Test 2", cik="0009991002")
    db_session.add(company)
    db_session.flush()

    tool = EarningsEstimatesTool(db_session)
    outcome = tool.run(EarningsEstimatesArgs(ticker="ZZEST2"))

    assert outcome.success
    assert outcome.data == {"available": False}
    assert "unavailable" in outcome.summary.lower()


def test_unknown_ticker_returns_empty(db_session):
    tool = EarningsEstimatesTool(db_session)
    outcome = tool.run(EarningsEstimatesArgs(ticker="NOSUCHTICKER"))

    assert outcome.success
    assert outcome.data == {}


def test_uses_most_recent_snapshot_for_the_period(db_session):
    company = Company(ticker="ZZEST3", name="ZZ Estimate Test 3", cik="0009991003")
    db_session.add(company)
    db_session.flush()
    db_session.add(_snapshot(company, snapshot_timestamp=datetime(2026, 1, 1, tzinfo=UTC)))
    db_session.add(
        _snapshot(
            company,
            snapshot_timestamp=datetime(2026, 3, 1, tzinfo=UTC),
            eps_estimate_average=Decimal("1.35"),
        )
    )
    db_session.flush()

    tool = EarningsEstimatesTool(db_session)
    outcome = tool.run(EarningsEstimatesArgs(ticker="ZZEST3"))

    assert outcome.data["eps_estimate_average"] == "1.350000"


class TestPointInTimeCutoff:
    """Phase 4 point-in-time hardening (2026-08-26), Section 20-21."""

    def test_snapshot_after_cutoff_excluded(self, db_session):
        company = Company(ticker="ZZEST4", name="ZZ Estimate Test 4", cik="0009991004")
        db_session.add(company)
        db_session.flush()
        db_session.add(
            _snapshot(
                company,
                snapshot_timestamp=datetime(2026, 1, 1, tzinfo=UTC),
                eps_estimate_average=Decimal("1.00"),
            )
        )
        db_session.add(
            _snapshot(
                company,
                snapshot_timestamp=datetime(2026, 6, 1, tzinfo=UTC),
                eps_estimate_average=Decimal("2.00"),
            )
        )
        db_session.flush()

        tool = EarningsEstimatesTool(db_session)
        outcome = tool.run(EarningsEstimatesArgs(ticker="ZZEST4", as_of=date(2026, 3, 1)))

        assert outcome.data["eps_estimate_average"] == "1.000000"

    def test_no_as_of_is_unrestricted_current_behavior(self, db_session):
        company = Company(ticker="ZZEST5", name="ZZ Estimate Test 5", cik="0009991005")
        db_session.add(company)
        db_session.flush()
        db_session.add(_snapshot(company))
        db_session.flush()

        tool = EarningsEstimatesTool(db_session)
        outcome = tool.run(EarningsEstimatesArgs(ticker="ZZEST5"))

        assert outcome.data["available"] is True
