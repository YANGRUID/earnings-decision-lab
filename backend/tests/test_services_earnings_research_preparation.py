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

from models.decision_snapshot import DecisionSnapshot
from models.earnings_calendar_event import EarningsCalendarEvent
from models.enums import EarningsCalendarEventStatus, EarningsTiming
from models.research_preparation_job import JobStatus, ResearchPreparationJob
from services.earnings_research_preparation import (
    enqueue_preparation_candidates,
    enqueue_ticker_for_preparation,
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


class TestEnqueue:
    def test_unresearched_eligible_company_is_enqueued_as_a_pending_row(
        self, db_session, options_provider
    ):
        event = _event(db_session, symbol="TESTNEW")

        results = enqueue_preparation_candidates(db_session, options_provider, now=FAR_FUTURE_NOW)

        assert len(results) == 1
        assert results[0].calendar_event_id == event.id
        assert results[0].symbol == "TESTNEW"
        assert results[0].outcome == "queued"
        assert results[0].reason is None

        row = db_session.query(ResearchPreparationJob).filter_by(ticker="TESTNEW").one()
        assert row.status == JobStatus.PENDING
        assert row.earnings_calendar_event_id == event.id
        assert row.attempt_count == 0

    def test_calling_it_twice_never_creates_a_duplicate_pending_row(
        self, db_session, options_provider
    ):
        """Idempotency at the enqueue layer itself, not just at
        prepare_company_research's own freshness gating below it -- a
        second scheduler scan (or a second admin on-demand trigger) must
        never pile up a second PENDING row for the same still-queued
        candidate."""
        _event(db_session, symbol="TESTDUP")

        first = enqueue_preparation_candidates(db_session, options_provider, now=FAR_FUTURE_NOW)
        second = enqueue_preparation_candidates(db_session, options_provider, now=FAR_FUTURE_NOW)

        assert first[0].outcome == "queued"
        assert second[0].outcome == "already_ready"
        assert db_session.query(ResearchPreparationJob).filter_by(ticker="TESTDUP").count() == 1

    @pytest.mark.parametrize(
        "status",
        [
            JobStatus.PENDING,
            JobStatus.RUNNING,
            JobStatus.INTERRUPTED,
            JobStatus.COMPLETED,
            JobStatus.COMPLETED_WITH_WARNINGS,
        ],
    )
    def test_a_prior_non_failed_job_is_never_re_enqueued(
        self, db_session, options_provider, status
    ):
        event = _event(db_session, symbol="TESTEXIST")
        prior = ResearchPreparationJob(
            ticker="TESTEXIST",
            earnings_calendar_event_id=event.id,
            status=status,
            steps=[],
            started_at=FAR_FUTURE_NOW - timedelta(days=1),
            attempt_count=0,
        )
        db_session.add(prior)
        _make_v4_ready(db_session, "TESTEXIST", FAR_FUTURE_NOW)
        db_session.commit()

        results = enqueue_preparation_candidates(db_session, options_provider, now=FAR_FUTURE_NOW)

        assert results[0].outcome == "already_ready"
        assert db_session.query(ResearchPreparationJob).filter_by(ticker="TESTEXIST").count() == 1

    def test_a_ready_job_reports_no_reason_but_an_in_progress_one_does(
        self, db_session, options_provider
    ):
        """already_ready's own `reason` field distinguishes "genuinely
        done, nothing more to do" (None) from "already in the queue/
        being worked, also nothing more to do right now" (a short human
        explanation) -- see enqueue_preparation_candidates's own
        docstring / _READY_JOB_STATUSES."""
        ready_event = _event(db_session, symbol="TESTREADY")
        db_session.add(
            ResearchPreparationJob(
                ticker="TESTREADY",
                earnings_calendar_event_id=ready_event.id,
                status=JobStatus.COMPLETED,
                steps=[],
                started_at=FAR_FUTURE_NOW - timedelta(days=1),
                attempt_count=1,
            )
        )
        _make_v4_ready(db_session, "TESTREADY", FAR_FUTURE_NOW)
        pending_event = _event(db_session, symbol="TESTPEND")
        db_session.add(
            ResearchPreparationJob(
                ticker="TESTPEND",
                earnings_calendar_event_id=pending_event.id,
                status=JobStatus.PENDING,
                steps=[],
                started_at=FAR_FUTURE_NOW - timedelta(days=1),
                attempt_count=0,
            )
        )
        db_session.commit()

        results = enqueue_preparation_candidates(db_session, options_provider, now=FAR_FUTURE_NOW)
        by_symbol = {r.symbol: r for r in results}

        assert by_symbol["TESTREADY"].outcome == "already_ready"
        assert by_symbol["TESTREADY"].reason is None
        assert by_symbol["TESTPEND"].outcome == "already_ready"
        assert by_symbol["TESTPEND"].reason == "already pending"

    def test_a_prior_failed_job_is_eligible_for_re_enqueue(self, db_session, options_provider):
        """A transient failure (e.g. a rate-limited provider) must not
        permanently block a real candidate from ever being retried on a
        later scan -- FAILED is deliberately excluded from
        _NO_REENQUEUE_STATUSES."""
        event = _event(db_session, symbol="TESTRETRY")
        db_session.add(
            ResearchPreparationJob(
                ticker="TESTRETRY",
                earnings_calendar_event_id=event.id,
                status=JobStatus.FAILED,
                steps=[],
                started_at=FAR_FUTURE_NOW - timedelta(days=1),
                attempt_count=3,
                error="simulated SEC EDGAR outage",
            )
        )
        db_session.commit()

        results = enqueue_preparation_candidates(db_session, options_provider, now=FAR_FUTURE_NOW)

        assert results[0].outcome == "queued"
        rows = db_session.query(ResearchPreparationJob).filter_by(ticker="TESTRETRY").all()
        assert len(rows) == 2  # the old FAILED row is preserved, a fresh PENDING row is added
        assert any(r.status == JobStatus.PENDING for r in rows)

    def test_enqueueing_never_creates_a_decision_snapshot(self, db_session, options_provider):
        _event(db_session, symbol="TESTNODECISION")
        before = db_session.query(DecisionSnapshot).count()

        enqueue_preparation_candidates(db_session, options_provider, now=FAR_FUTURE_NOW)

        after = db_session.query(DecisionSnapshot).count()
        assert after == before  # unchanged -- enqueueing never touches this table

    def test_one_events_ineligibility_does_not_block_the_next_candidate(
        self, db_session, options_provider
    ):
        _event(db_session, symbol="TESTFILTERED", market_cap="500000000")
        event_ok = _event(db_session, symbol="TESTOK", market_cap="80000000000")

        results = enqueue_preparation_candidates(db_session, options_provider, now=FAR_FUTURE_NOW)
        by_symbol = {r.symbol: r for r in results}

        assert by_symbol["TESTFILTERED"].outcome == "filtered_out"
        assert by_symbol["TESTOK"].outcome == "queued"
        assert by_symbol["TESTOK"].calendar_event_id == event_ok.id


class TestEnqueueTickerForPreparation:
    """AI Research architecture fix (2026-08-26), Part A4 -- the on-demand,
    non-calendar-driven counterpart to enqueue_preparation_candidates
    above, reusing the exact same durable ResearchPreparationJob table
    and worker."""

    def test_queues_a_durable_row_with_no_calendar_event(self, db_session):
        job = enqueue_ticker_for_preparation(db_session, "TESTONDEMAND", now=FAR_FUTURE_NOW)

        assert job.id is not None
        assert job.ticker == "TESTONDEMAND"
        assert job.earnings_calendar_event_id is None
        assert job.status == JobStatus.PENDING

    def test_reuses_an_existing_pending_row_instead_of_duplicating(self, db_session):
        first = enqueue_ticker_for_preparation(db_session, "TESTDUP", now=FAR_FUTURE_NOW)

        second = enqueue_ticker_for_preparation(db_session, "TESTDUP", now=FAR_FUTURE_NOW)

        assert second.id == first.id
        rows = db_session.query(ResearchPreparationJob).filter_by(ticker="TESTDUP").all()
        assert len(rows) == 1

    def test_reuses_a_running_calendar_driven_row_for_the_same_ticker(
        self, db_session, options_provider
    ):
        _event(db_session, symbol="TESTSHARED")
        enqueue_preparation_candidates(db_session, options_provider, now=FAR_FUTURE_NOW)
        calendar_job = db_session.query(ResearchPreparationJob).filter_by(ticker="TESTSHARED").one()
        calendar_job.status = JobStatus.RUNNING
        db_session.commit()

        job = enqueue_ticker_for_preparation(db_session, "TESTSHARED", now=FAR_FUTURE_NOW)

        assert job.id == calendar_job.id
        rows = db_session.query(ResearchPreparationJob).filter_by(ticker="TESTSHARED").all()
        assert len(rows) == 1

    def test_enqueues_a_fresh_row_when_a_prior_one_already_failed(self, db_session):
        enqueue_ticker_for_preparation(db_session, "TESTREENQ", now=FAR_FUTURE_NOW)
        stale = db_session.query(ResearchPreparationJob).filter_by(ticker="TESTREENQ").one()
        stale.status = JobStatus.FAILED
        db_session.commit()

        job = enqueue_ticker_for_preparation(db_session, "TESTREENQ", now=FAR_FUTURE_NOW)

        assert job.id != stale.id
        assert job.status == JobStatus.PENDING

    def test_never_creates_a_decision_snapshot(self, db_session):
        before = db_session.query(DecisionSnapshot).count()

        enqueue_ticker_for_preparation(db_session, "TESTNODECISION2", now=FAR_FUTURE_NOW)

        after = db_session.query(DecisionSnapshot).count()
        assert after == before
