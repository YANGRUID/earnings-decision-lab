"""Pre-live hardening (2026-08-25) -- tests for services/earnings_
research_preparation.py. This module now owns exactly one job: cheap-
filter upcoming calendar events, then ENQUEUE a durable
ResearchPreparationJob row for each real survivor -- it never runs the
actual (network/CPU-heavy) preparation pipeline itself any more (see
that module's own docstring for the 2026-08-25 architecture change: the
dedicated research-worker process, services/research_preparation_queue.py,
now owns that). These tests accordingly never monkeypatch
prepare_company_research -- there is nothing of it left to call here.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from models.earnings_calendar_event import EarningsCalendarEvent
from models.enums import EarningsCalendarEventStatus, EarningsTiming
from models.research_preparation_job import ResearchPreparationJob
from services.earnings_research_preparation import (
    enqueue_preparation_candidates,
)

# Far-future so this suite's own candidate-window queries never collide
# with anything else -- this project's own established convention (see
# tests/test_services_operations.py's own FAR_FUTURE_EARNINGS_DATE).
# FAR_FUTURE_NOW sits 2 days before FAR_FUTURE_DATE, inside the real
# PREPARATION_LOOKAHEAD_DAYS(5) window -- candidate_events_for_
# preparation() is a plain calendar-date bound relative to ``now``, so
# tests must keep the two in sync rather than using real wall-clock time.
FAR_FUTURE_DATE = date(2033, 4, 12)
FAR_FUTURE_NOW = datetime(2033, 4, 10, tzinfo=UTC)


def _event(db_session, *, symbol, market_cap="50000000000", country="US", earnings_date=None):
    """Committed, not just flushed -- a real EarningsCalendarEvent row
    the preparation job would actually process is always already
    committed (synced by a separate, earlier job run)."""
    event = EarningsCalendarEvent(
        symbol=symbol,
        company_name=f"Test {symbol} Co",
        earnings_date=earnings_date or FAR_FUTURE_DATE,
        earnings_time=EarningsTiming.AMC,
        status=EarningsCalendarEventStatus.UPCOMING,
        market_cap=Decimal(market_cap) if market_cap is not None else None,
        country=country,
    )
    db_session.add(event)
    db_session.commit()
    return event



def _make_v4_ready(db, symbol, now):
    """V4-only reset (2026-09-02): a completed preparation job counts as
    'nothing more to do' only when the company is actually V4-ready --
    a Company row with a fresh AI thesis."""
    from models.ai_thesis_version import AIThesisVersion
    from models.company import Company

    company = db.query(Company).filter_by(ticker=symbol).one_or_none()
    if company is None:
        company = Company(ticker=symbol, name=f"{symbol} Inc")
        db.add(company)
        db.flush()
    db.add(
        AIThesisVersion(
            company_id=company.id,
            business_context="b",
            historical_earnings_pattern="h",
            guidance_trend="g",
            key_risks="k",
            market_setup="m",
            disclaimer="d",
            citations=[],
            provider="deepseek",
            model="deepseek-v4-flash",
            created_at=now - timedelta(hours=1),
        )
    )
    db.flush()

class _FakeOptionsProvider:
    """A real options-chain check_eligibility can call cheaply -- always
    reports a tradable expiration, so eligibility here is governed
    entirely by market_cap/country, matching what these tests want to
    isolate."""

    def list_available_expirations(self, symbol, after):
        return [after + timedelta(days=30)]


class _RateLimitedOptionsProvider:
    """A real, transient provider-call failure (e.g. IBKR rate limiting)
    -- distinct from a genuine, data-driven ineligibility verdict. See
    services/earnings_eligibility.py::EligibilityResult.retryable's own
    docstring for the real Aug 25 (WSM) evidence this exists to fix."""

    def list_available_expirations(self, symbol, after):
        raise RuntimeError("IBKR Client Portal Gateway rate-limited the request")


@pytest.fixture
def options_provider():
    return _FakeOptionsProvider()


class TestCheapFilterFirst:
    def test_market_cap_below_threshold_never_gets_enqueued(self, db_session, options_provider):
        _event(db_session, symbol="TESTSMALL", market_cap="500000000")  # $500M, below $10B

        results = enqueue_preparation_candidates(db_session, options_provider, now=FAR_FUTURE_NOW)

        assert len(results) == 1
        assert results[0].outcome == "filtered_out"
        assert "market cap" in results[0].reason
        assert db_session.query(ResearchPreparationJob).filter_by(ticker="TESTSMALL").count() == 0

    def test_non_us_listing_never_gets_enqueued(self, db_session, options_provider):
        _event(db_session, symbol="TESTINTL", country="DE")

        results = enqueue_preparation_candidates(db_session, options_provider, now=FAR_FUTURE_NOW)

        assert results[0].outcome == "filtered_out"
        assert "US listed" in results[0].reason
        assert db_session.query(ResearchPreparationJob).filter_by(ticker="TESTINTL").count() == 0

    def test_events_outside_the_lookahead_window_are_not_even_candidates(
        self, db_session, options_provider
    ):
        now = datetime(2033, 1, 1, tzinfo=UTC)
        _event(db_session, symbol="TESTFAR", earnings_date=date(2033, 6, 1))  # far beyond window

        results = enqueue_preparation_candidates(db_session, options_provider, now=now)

        assert results == []

    def test_a_transient_provider_failure_is_a_warning_not_a_hard_filter(self, db_session):
        """Post-live correction (2026-08-25): a rate-limited (or
        otherwise transiently failing) options-chain probe must not be
        recorded the same way as a genuine, permanent ineligibility
        verdict (market cap, non-US listing) -- see EligibilityResult.
        retryable's own docstring for the real Aug 25 WSM evidence."""
        _event(db_session, symbol="TESTWARN")

        results = enqueue_preparation_candidates(
            db_session, _RateLimitedOptionsProvider(), now=FAR_FUTURE_NOW
        )

        assert len(results) == 1
        assert results[0].outcome == "preparation_warning"
        assert "rate-limited" in results[0].reason
        # Not enqueued this scan either way (same as filtered_out) -- but
        # nothing here is sticky: the next scan (no job row exists to
        # dedupe against) will call check_eligibility fresh again.
        assert db_session.query(ResearchPreparationJob).filter_by(ticker="TESTWARN").count() == 0




