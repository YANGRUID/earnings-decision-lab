"""Pre-live hardening (2026-08-25) -- tests for services/research_
preparation_queue.py: atomic claiming (including a real, two-connection
``FOR UPDATE SKIP LOCKED`` concurrency test -- not simulated), lease/
heartbeat-based stale-RUNNING-row recovery, and the live queue-depth
count Operations reads. See that module's own docstring for why this is
a real Postgres property being verified, not an in-memory assumption.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from conftest import engine
from sqlalchemy.orm import Session

from models.earnings_calendar_event import EarningsCalendarEvent
from models.enums import EarningsCalendarEventStatus, EarningsTiming
from models.research_preparation_job import JobStatus, ResearchPreparationJob
from services.research_preparation_queue import (
    MAX_ATTEMPTS,
    claim_next_preparation_job,
    count_queue_depth,
    recover_stale_running_jobs,
)

NOW = datetime(2033, 5, 1, tzinfo=UTC)


def _event(db_session, *, symbol, earnings_date=None):
    event = EarningsCalendarEvent(
        symbol=symbol,
        company_name=f"Test {symbol} Co",
        earnings_date=earnings_date or date(2033, 5, 5),
        earnings_time=EarningsTiming.AMC,
        status=EarningsCalendarEventStatus.UPCOMING,
        market_cap=Decimal("50000000000"),
        country="US",
    )
    db_session.add(event)
    db_session.commit()
    return event


def _job(
    db_session,
    *,
    ticker,
    status,
    event_id=None,
    heartbeat_at=None,
    attempt_count=0,
    started_at=None,
    worker_id=None,
):
    job = ResearchPreparationJob(
        ticker=ticker,
        earnings_calendar_event_id=event_id,
        status=status,
        steps=[],
        started_at=started_at or NOW,
        heartbeat_at=heartbeat_at,
        attempt_count=attempt_count,
        worker_id=worker_id,
    )
    db_session.add(job)
    db_session.commit()
    return job


class TestClaimNextPreparationJob:
    def test_claims_a_pending_row_and_sets_ownership_fields(self, db_session):
        event = _event(db_session, symbol="TESTCLAIM")
        job = _job(db_session, ticker="TESTCLAIM", status=JobStatus.PENDING, event_id=event.id)

        claimed = claim_next_preparation_job(db_session, "worker-a", now=NOW)

        assert claimed is not None
        assert claimed.id == job.id
        assert claimed.status == JobStatus.RUNNING
        assert claimed.worker_id == "worker-a"
        assert claimed.heartbeat_at == NOW
        assert claimed.attempt_count == 1
        assert claimed.error is None

    def test_claims_an_interrupted_row_too(self, db_session):
        event = _event(db_session, symbol="TESTINTR")
        _job(
            db_session,
            ticker="TESTINTR",
            status=JobStatus.INTERRUPTED,
            event_id=event.id,
            attempt_count=1,
            heartbeat_at=NOW - timedelta(minutes=10),
        )

        claimed = claim_next_preparation_job(db_session, "worker-a", now=NOW)

        assert claimed is not None
        assert claimed.status == JobStatus.RUNNING
        assert claimed.attempt_count == 2  # incremented, not reset

    def test_returns_none_when_nothing_is_claimable(self, db_session):
        event = _event(db_session, symbol="TESTNONE")
        _job(db_session, ticker="TESTNONE", status=JobStatus.RUNNING, event_id=event.id)

        assert claim_next_preparation_job(db_session, "worker-a", now=NOW) is None

    def test_never_claims_a_row_at_or_past_max_attempts(self, db_session):
        event = _event(db_session, symbol="TESTMAXED")
        _job(
            db_session,
            ticker="TESTMAXED",
            status=JobStatus.INTERRUPTED,
            event_id=event.id,
            attempt_count=MAX_ATTEMPTS,
        )

        assert claim_next_preparation_job(db_session, "worker-a", now=NOW) is None

    def test_soonest_earnings_date_is_claimed_first(self, db_session):
        soon_event = _event(db_session, symbol="TESTSOON", earnings_date=date(2033, 5, 2))
        later_event = _event(db_session, symbol="TESTLATER", earnings_date=date(2033, 5, 20))
        _job(db_session, ticker="TESTLATER", status=JobStatus.PENDING, event_id=later_event.id)
        _job(db_session, ticker="TESTSOON", status=JobStatus.PENDING, event_id=soon_event.id)

        claimed = claim_next_preparation_job(db_session, "worker-a", now=NOW)

        assert claimed is not None
        assert claimed.ticker == "TESTSOON"

    def test_a_row_with_no_linked_event_sorts_last_not_first(self, db_session):
        later_event = _event(db_session, symbol="TESTLINKED", earnings_date=date(2033, 5, 20))
        _job(db_session, ticker="TESTUNLINKED", status=JobStatus.PENDING, event_id=None)
        _job(db_session, ticker="TESTLINKED", status=JobStatus.PENDING, event_id=later_event.id)

        claimed = claim_next_preparation_job(db_session, "worker-a", now=NOW)

        assert claimed is not None
        assert claimed.ticker == "TESTLINKED"


class TestConcurrentClaiming:
    def test_two_real_connections_never_claim_the_same_row(self):
        """The real property this architecture depends on: ``FOR UPDATE
        SKIP LOCKED`` across two genuinely separate database connections/
        transactions, not simulated with threads or mocks. Connection A
        claims (and, by locking the row inside its still-open
        transaction, holds it) before connection B's own claim attempt
        runs -- B must see nothing claimable, exactly as it would if a
        second real worker process polled at the same instant.

        Deliberately does NOT use the ``db_session`` fixture: that
        session is joined to its connection's transaction via a
        SAVEPOINT (see conftest.py's own docstring), so
        ``db_session.commit()`` only releases the savepoint -- the outer
        transaction stays open until test teardown, meaning a row
        inserted through it would never actually become visible to a
        genuinely separate connection at all. Every connection here is
        therefore its own real, independently-committed one, with
        explicit cleanup at the end since nothing rolls this back
        automatically."""
        event_id: int
        with engine.connect() as setup_conn:
            setup_txn = setup_conn.begin()
            setup_session = Session(bind=setup_conn)
            event = EarningsCalendarEvent(
                symbol="TESTCONCUR",
                company_name="Test TESTCONCUR Co",
                earnings_date=date(2033, 5, 5),
                earnings_time=EarningsTiming.AMC,
                status=EarningsCalendarEventStatus.UPCOMING,
                market_cap=Decimal("50000000000"),
                country="US",
            )
            setup_session.add(event)
            setup_session.flush()
            event_id = event.id
            setup_session.add(
                ResearchPreparationJob(
                    ticker="TESTCONCUR",
                    earnings_calendar_event_id=event_id,
                    status=JobStatus.PENDING,
                    steps=[],
                    started_at=NOW,
                    attempt_count=0,
                )
            )
            setup_session.commit()
            setup_txn.commit()

        conn_a = engine.connect()
        conn_b = engine.connect()
        txn_a = conn_a.begin()
        txn_b = conn_b.begin()
        session_a = Session(bind=conn_a)
        session_b = Session(bind=conn_b)
        try:
            claimed_by_a = claim_next_preparation_job(session_a, "worker-a", now=NOW)
            assert claimed_by_a is not None
            assert claimed_by_a.ticker == "TESTCONCUR"

            # A's transaction is still open (not committed) -- its FOR
            # UPDATE lock on the row is nonetheless already held, which
            # is exactly what makes B's skip_locked query skip it.
            claimed_by_b = claim_next_preparation_job(session_b, "worker-b", now=NOW)
            assert claimed_by_b is None
        finally:
            session_a.close()
            session_b.close()
            txn_a.rollback()
            txn_b.rollback()
            conn_a.close()
            conn_b.close()
            with engine.connect() as cleanup_conn:
                cleanup_txn = cleanup_conn.begin()
                cleanup_session = Session(bind=cleanup_conn)
                cleanup_session.query(ResearchPreparationJob).filter_by(
                    ticker="TESTCONCUR"
                ).delete()
                cleanup_session.query(EarningsCalendarEvent).filter_by(id=event_id).delete()
                cleanup_session.commit()
                cleanup_txn.commit()


class TestRecoverStaleRunningJobs:
    def test_a_running_row_with_a_stale_heartbeat_is_recovered_to_interrupted(self, db_session):
        event = _event(db_session, symbol="TESTSTALE")
        job = _job(
            db_session,
            ticker="TESTSTALE",
            status=JobStatus.RUNNING,
            event_id=event.id,
            attempt_count=1,
            heartbeat_at=NOW - timedelta(minutes=10),
        )

        result = recover_stale_running_jobs(db_session, now=NOW, lease_timeout=timedelta(minutes=5))

        assert result.recovered_to_interrupted == 1
        assert result.permanently_failed == 0
        db_session.refresh(job)
        assert job.status == JobStatus.INTERRUPTED
        assert "reclaimable" in job.error

    def test_a_running_row_with_no_heartbeat_at_all_is_also_recovered(self, db_session):
        """Defensive: should never happen once claim_next_preparation_
        job always sets heartbeat_at on claim, but treated the same way
        as stale rather than assumed healthy."""
        event = _event(db_session, symbol="TESTNOHB")
        job = _job(
            db_session,
            ticker="TESTNOHB",
            status=JobStatus.RUNNING,
            event_id=event.id,
            attempt_count=1,
            heartbeat_at=None,
        )

        result = recover_stale_running_jobs(db_session, now=NOW)

        assert result.recovered_to_interrupted == 1
        db_session.refresh(job)
        assert job.status == JobStatus.INTERRUPTED

    def test_a_running_row_that_has_exhausted_max_attempts_is_marked_failed_not_interrupted(
        self, db_session
    ):
        event = _event(db_session, symbol="TESTEXHAUST")
        job = _job(
            db_session,
            ticker="TESTEXHAUST",
            status=JobStatus.RUNNING,
            event_id=event.id,
            attempt_count=MAX_ATTEMPTS,
            heartbeat_at=NOW - timedelta(minutes=10),
        )

        result = recover_stale_running_jobs(db_session, now=NOW)

        assert result.recovered_to_interrupted == 0
        assert result.permanently_failed == 1
        db_session.refresh(job)
        assert job.status == JobStatus.FAILED
        assert job.completed_at == NOW

    def test_a_running_row_with_a_recent_heartbeat_is_left_alone(self, db_session):
        event = _event(db_session, symbol="TESTFRESH")
        job = _job(
            db_session,
            ticker="TESTFRESH",
            status=JobStatus.RUNNING,
            event_id=event.id,
            attempt_count=1,
            heartbeat_at=NOW - timedelta(seconds=10),
        )

        result = recover_stale_running_jobs(db_session, now=NOW, lease_timeout=timedelta(minutes=5))

        assert result.recovered_to_interrupted == 0
        assert result.permanently_failed == 0
        db_session.refresh(job)
        assert job.status == JobStatus.RUNNING

    def test_non_running_rows_are_never_touched(self, db_session):
        event = _event(db_session, symbol="TESTPEND")
        job = _job(
            db_session,
            ticker="TESTPEND",
            status=JobStatus.PENDING,
            event_id=event.id,
        )

        result = recover_stale_running_jobs(db_session, now=NOW)

        assert result.recovered_to_interrupted == 0
        db_session.refresh(job)
        assert job.status == JobStatus.PENDING

    def test_a_reclaimed_row_can_then_be_claimed_by_a_different_worker(self, db_session):
        """End-to-end: the exact restart-recovery path Section 13's
        crash-acceptance scenario depends on -- a worker dies mid-job,
        the next recovery sweep reclaims it, and a (possibly different)
        worker picks it back up."""
        event = _event(db_session, symbol="TESTRESUME")
        _job(
            db_session,
            ticker="TESTRESUME",
            status=JobStatus.RUNNING,
            event_id=event.id,
            attempt_count=1,
            heartbeat_at=NOW - timedelta(minutes=10),
            worker_id="research-worker-dead",
        )

        recover_stale_running_jobs(db_session, now=NOW)
        reclaimed = claim_next_preparation_job(db_session, "research-worker-new", now=NOW)

        assert reclaimed is not None
        assert reclaimed.ticker == "TESTRESUME"
        assert reclaimed.status == JobStatus.RUNNING
        assert reclaimed.worker_id == "research-worker-new"
        assert reclaimed.attempt_count == 2


class TestCountQueueDepth:
    def test_counts_only_claimable_rows(self, db_session):
        """Delta, not an absolute count: this disposable test database is
        shared with the rest of this pytest run (and, per conftest.py's
        own docstring, with the Playwright E2E suite too), so the only
        honest assertion is "count_queue_depth grew by exactly what this
        test itself added" -- never "the table is empty except for what
        I just inserted"."""
        before = count_queue_depth(db_session)

        event = _event(db_session, symbol="TESTDEPTH")
        _job(db_session, ticker="TESTDEPTH1", status=JobStatus.PENDING, event_id=event.id)
        _job(db_session, ticker="TESTDEPTH2", status=JobStatus.INTERRUPTED, event_id=event.id)
        _job(db_session, ticker="TESTDEPTH3", status=JobStatus.RUNNING, event_id=event.id)
        _job(db_session, ticker="TESTDEPTH4", status=JobStatus.COMPLETED, event_id=event.id)
        _job(db_session, ticker="TESTDEPTH5", status=JobStatus.FAILED, event_id=event.id)
        _job(
            db_session,
            ticker="TESTDEPTH6",
            status=JobStatus.PENDING,
            event_id=event.id,
            attempt_count=MAX_ATTEMPTS,
        )

        assert count_queue_depth(db_session) == before + 2  # only TESTDEPTH1 and TESTDEPTH2
