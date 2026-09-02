"""Phase 4.2/4.4/4.5/4.8A/4.9 -- tests for services/scheduler.py: all
four jobs are registered correctly, api/main.py's real lifespan
starts/stops them gracefully, and GET /system-status's scheduler field
(Phase 4.9) reflects real, live scheduler state -- never assumed."""

import logging
import time
from datetime import timedelta

import pytest
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED, JobExecutionEvent
from fastapi.testclient import TestClient
from sqlalchemy import func

from core.config import Settings
from models.enums import AnnouncementTime, EarningsTiming
from models.scheduler_run import SchedulerRun, SchedulerRunEvent
from services.scheduler import (
    CALENDAR_SYNC_JOB_ID,
    EARNINGS_RESEARCH_PREPARATION_JOB_ID,
    IBKR_GATEWAY_HEALTHCHECK_JOB_ID,
    build_scheduler,
    get_scheduler_status,
    run_earnings_calendar_sync_job,
    run_ibkr_gateway_healthcheck_job,
)

# Captured at module-import time, before conftest.py's autouse
# _no_real_sleeps fixture ever runs -- a genuine, unpatchable-by-that-
# fixture reference to the real time.sleep, for the one test
# (TestMultiCompanyThroughput) that needs to measure real elapsed
# wall-clock time rather than have it faked away.
_REAL_SLEEP = time.sleep

_TIMING_TO_ANNOUNCEMENT = {
    EarningsTiming.BMO: AnnouncementTime.BEFORE_MARKET,
    EarningsTiming.AMC: AnnouncementTime.AFTER_MARKET,
}



def _expected_registered_ids() -> set[str]:
    """The official five, plus the V4 shadow pair exactly when the running
    environment has activated the cohort (production, 2026-09-02). The
    scheduler registers the pair only while V4_SHADOW_ENABLED is on."""
    from core.config import get_settings
    from services.scheduler import (
        RESEARCH_PREPARATION_STARTUP_CATCHUP_JOB_ID,
        RESEARCH_READINESS_CATCHUP_JOB_ID,
    )

    ids = {
        CALENDAR_SYNC_JOB_ID,
        EARNINGS_RESEARCH_PREPARATION_JOB_ID,
        RESEARCH_READINESS_CATCHUP_JOB_ID,
        RESEARCH_PREPARATION_STARTUP_CATCHUP_JOB_ID,
        IBKR_GATEWAY_HEALTHCHECK_JOB_ID,
    }
    if get_settings().v4_shadow_enabled:
        ids |= {"v4_shadow_decision", "v4_shadow_settlement"}
    return ids



@pytest.fixture
def rollback_safe_session_local(monkeypatch, db_session):
    """Points services.scheduler.SessionLocal at the test's own
    db_session instead of a real, separately-committed connection to the
    shared dev Postgres instance -- every job function below calls
    SessionLocal() itself (never accepts a session as an argument,
    matching this project's own established pattern), so without this,
    calling a job body directly in a test would write real, permanent
    rows exactly like tests/test_api_admin.py's own docstring already
    warns about for the job-triggering endpoints. ``db.close()`` is
    patched to a no-op so the job's own ``finally: db.close()`` doesn't
    detach the fixture's connection out from under the test.

    Explicit real cleanup, not just db_session's own savepoint rollback:
    confirmed live that a scheduler_run/scheduler_run_event row survives
    db_session's rollback once a test calls db.commit() more than once
    in the same session (start_scheduler_run + finish_scheduler_run
    always do exactly that) -- the job bodies' own multiple, real
    commits are real production behavior worth keeping exactly as they
    are, so the fix belongs here, in the test fixture, not there.

    The id floor captured here is also stashed on the session itself
    (``scheduler_run_floor``) for test bodies to scope their own
    ``.query(SchedulerRun)``/``.query(SchedulerRunEvent)`` assertions --
    confirmed live that the real backend's own scheduler can commit a
    real row to this exact shared table while this suite runs (it did:
    a real ibkr_gateway_healthcheck run landed mid-session), which turns
    a bare ``.one()`` into ``MultipleResultsFound``. See
    the_scheduler_run()/the_scheduler_run_events() below."""
    monkeypatch.setattr(db_session, "close", lambda: None)
    monkeypatch.setattr("services.scheduler.SessionLocal", lambda: db_session)
    max_run_id_before = db_session.query(func.max(SchedulerRun.id)).scalar() or 0
    db_session.scheduler_run_floor = max_run_id_before
    try:
        yield db_session
    finally:
        db_session.query(SchedulerRunEvent).filter(
            SchedulerRunEvent.scheduler_run_id > max_run_id_before
        ).delete(synchronize_session=False)
        db_session.query(SchedulerRun).filter(SchedulerRun.id > max_run_id_before).delete(
            synchronize_session=False
        )
        db_session.commit()


def the_scheduler_run(session):
    """The one SchedulerRun row this test itself created, scoped past
    ``scheduler_run_floor`` (see rollback_safe_session_local above) so a
    real, already-committed row from the live backend can never make
    this ambiguous."""
    return session.query(SchedulerRun).filter(SchedulerRun.id > session.scheduler_run_floor).one()


def the_scheduler_run_events(session, **filters):
    """SchedulerRunEvent rows this test itself created, scoped past
    ``scheduler_run_floor`` and optionally further filtered (by symbol,
    stage, etc.) -- same real-row-safety rationale as the_scheduler_run
    above."""
    return (
        session.query(SchedulerRunEvent)
        .filter(SchedulerRunEvent.scheduler_run_id > session.scheduler_run_floor)
        .filter_by(**filters)
        .all()
    )


def test_calendar_sync_job_registered_correctly():
    """Unstarted on purpose -- AsyncIOScheduler.start() needs a running
    asyncio event loop (see the other two tests below, both driven
    through the real app lifespan), which a plain sync test doesn't have.
    Job registration itself doesn't need the scheduler to be running."""
    scheduler = build_scheduler()

    job = scheduler.get_job(CALENDAR_SYNC_JOB_ID)
    assert job is not None
    assert job.func is not None
    assert job.func.__name__ == "run_earnings_calendar_sync_job"

    field_by_name = {field.name: field for field in job.trigger.fields}
    assert str(field_by_name["hour"]) == "0"
    assert str(field_by_name["minute"]) == "0"


def test_ibkr_gateway_healthcheck_job_registered_correctly():
    """Phase 4.8A: a real interval trigger (every IBKR_HEALTHCHECK_
    INTERVAL_MINUTES, all day) -- unlike the other three jobs, this one
    isn't anchored to a single daily wall-clock moment, since its whole
    purpose is to catch a dead/idle session well before the next
    scheduled capture window (see the job's own docstring)."""
    scheduler = build_scheduler()

    job = scheduler.get_job(IBKR_GATEWAY_HEALTHCHECK_JOB_ID)
    assert job is not None
    assert job.func.__name__ == "run_ibkr_gateway_healthcheck_job"
    assert job.args == ()
    assert job.trigger.interval.total_seconds() == 600  # 10 minutes






class TestIbkrGatewayHealthcheckJob:
    """Phase 4.8A -- the job body itself (registration is covered above).
    Real HTTP mocked via httpx_mock, exactly like
    test_providers_ibkr_client.py, rather than reaching a live Gateway."""

    def _settings(self, *, options_provider: str = "ibkr") -> Settings:
        return Settings(
            options_provider=options_provider,
            ibkr_base_url="https://localhost:5001/v1/api",
            _env_file=None,
        )

    def test_skips_when_options_provider_is_not_ibkr(
        self, monkeypatch, caplog, rollback_safe_session_local
    ):
        import services.scheduler as scheduler_module

        monkeypatch.setattr(
            scheduler_module,
            "get_settings",
            lambda: self._settings(options_provider="alpha_vantage"),
        )
        with caplog.at_level(logging.DEBUG, logger="services.scheduler"):
            run_ibkr_gateway_healthcheck_job()

        assert "skipped" in caplog.text
        run = the_scheduler_run(rollback_safe_session_local)
        assert run.job_id == IBKR_GATEWAY_HEALTHCHECK_JOB_ID
        assert run.status == "skipped"

    def test_logs_info_when_connected(
        self, monkeypatch, httpx_mock, caplog, rollback_safe_session_local
    ):
        import services.scheduler as scheduler_module

        monkeypatch.setattr(scheduler_module, "get_settings", lambda: self._settings())
        httpx_mock.add_response(
            url="https://localhost:5001/v1/api/iserver/auth/status",
            json={"authenticated": True, "connected": True, "competing": False},
        )

        with caplog.at_level(logging.INFO, logger="services.scheduler"):
            run_ibkr_gateway_healthcheck_job()

        assert "CONNECTED" in caplog.text
        assert not any(record.levelno >= logging.WARNING for record in caplog.records)
        run = the_scheduler_run(rollback_safe_session_local)
        assert run.status == "success"
        assert run.finished_at is not None
        assert run.duration_ms is not None

    def test_logs_warning_when_not_authenticated(
        self, monkeypatch, httpx_mock, caplog, rollback_safe_session_local
    ):
        import services.scheduler as scheduler_module

        monkeypatch.setattr(scheduler_module, "get_settings", lambda: self._settings())
        httpx_mock.add_response(
            url="https://localhost:5001/v1/api/iserver/auth/status",
            json={"authenticated": False, "connected": False, "competing": False},
        )

        with caplog.at_level(logging.INFO, logger="services.scheduler"):
            run_ibkr_gateway_healthcheck_job()

        assert "AUTH_REQUIRED" in caplog.text
        assert any(record.levelno == logging.WARNING for record in caplog.records)
        run = the_scheduler_run(rollback_safe_session_local)
        assert run.status == "error"
        assert run.error_summary == "AUTH_REQUIRED"

    def test_logs_warning_when_gateway_unreachable(
        self, monkeypatch, httpx_mock, caplog, rollback_safe_session_local
    ):
        """Reconnect-detection scenario: the Gateway is down entirely (not
        just unauthenticated) -- the job must still log a clear, honest
        signal, never raise, and never crash the scheduler."""
        import httpx

        import services.scheduler as scheduler_module

        monkeypatch.setattr(scheduler_module, "get_settings", lambda: self._settings())
        httpx_mock.add_exception(httpx.ConnectError("connection refused"))

        with caplog.at_level(logging.INFO, logger="services.scheduler"):
            run_ibkr_gateway_healthcheck_job()  # must not raise

        assert "GATEWAY_UNREACHABLE" in caplog.text
        assert any(record.levelno == logging.WARNING for record in caplog.records)
        run = the_scheduler_run(rollback_safe_session_local)
        assert run.status == "error"
        assert "GATEWAY_UNREACHABLE" in run.error_summary


class TestCalendarSyncJobTracking:
    """Operations Monitor -- services/scheduler_run_tracking.py's real,
    persisted job-run history for run_earnings_calendar_sync_job, the
    one real gap this project's own scheduler had before (see
    models/scheduler_run.py's own docstring): no run of any job left a
    durable record of what happened, only two in-memory dicts reset on
    every restart."""

    def test_records_a_successful_run_with_real_counts(
        self, monkeypatch, rollback_safe_session_local
    ):
        import services.scheduler as scheduler_module
        from services.earnings_calendar_sync import EarningsCalendarSyncResult

        fake_result = EarningsCalendarSyncResult(
            fetched=3, created=1, updated=1, unchanged=1, dates_fetched=1, dates_skipped=13
        )
        monkeypatch.setattr(
            scheduler_module, "build_earnings_calendar_provider", lambda settings, db: object()
        )
        monkeypatch.setattr(
            scheduler_module,
            "sync_earnings_calendar",
            lambda db, provider, from_date=None: fake_result,
        )

        run_earnings_calendar_sync_job()

        run = the_scheduler_run(rollback_safe_session_local)
        assert run.job_id == CALENDAR_SYNC_JOB_ID
        assert run.status == "success"
        assert run.items_evaluated == 3
        assert run.items_succeeded == 3  # created + updated + unchanged, none failed
        assert run.items_failed == 0
        assert run.finished_at is not None
        assert run.duration_ms is not None

    def test_records_skipped_when_no_provider_configured(
        self, monkeypatch, rollback_safe_session_local
    ):
        import services.scheduler as scheduler_module

        monkeypatch.setattr(
            scheduler_module, "build_earnings_calendar_provider", lambda settings, db: None
        )

        run_earnings_calendar_sync_job()

        run = the_scheduler_run(rollback_safe_session_local)
        assert run.status == "skipped"

    def test_records_error_and_redacts_a_secret_looking_message(
        self, monkeypatch, rollback_safe_session_local
    ):
        """A raw provider exception could echo a query-string API key
        (real, confirmed leak path -- see observability/redact.py's own
        docstring) -- this job's error_summary must never store it
        un-redacted."""
        import services.scheduler as scheduler_module

        monkeypatch.setattr(
            scheduler_module, "build_earnings_calendar_provider", lambda settings, db: object()
        )

        def _raise(db, provider, from_date=None):
            raise RuntimeError("call failed: https://api.example.com/x?apikey=SECRET123")

        monkeypatch.setattr(scheduler_module, "sync_earnings_calendar", _raise)

        run_earnings_calendar_sync_job()  # must not raise -- pre-existing behavior

        run = the_scheduler_run(rollback_safe_session_local)
        assert run.status == "error"
        assert "SECRET123" not in run.error_summary
        assert "REDACTED" in run.error_summary


class TestEarningsResearchPreparationJobTracking:
    """Pre-live hardening (2026-08-25) -- services/scheduler_run_
    tracking.py's real, persisted job-run history for run_earnings_
    research_preparation_job. This job now only ENQUEUES durable
    ResearchPreparationJob rows (services/earnings_research_
    preparation.py::enqueue_preparation_candidates) -- it no longer runs
    any real preparation pipeline itself, so it no longer depends on
    ``_shared_embedder`` at all (the real network/CPU-heavy work moved
    to the dedicated research-worker process, see workers/research_
    preparation_worker.py + services/research_preparation_queue.py)."""

    class _FakeOptionsProvider:
        """Same cheap-eligibility-check shape as tests/test_services_
        earnings_research_preparation.py's own fake -- always reports a
        tradable expiration, so eligibility here is governed entirely by
        each test event's own market_cap/country."""

        def list_available_expirations(self, symbol, after):
            return [after + timedelta(days=30)]

    def test_real_enqueue_creates_pending_rows_and_returns_them(
        self, monkeypatch, rollback_safe_session_local, db_session
    ):
        """End-to-end through the real, unmocked enqueue_preparation_
        candidates -- only the options-provider chain (a real external
        API client) is swapped for a fake, matching how the scheduler
        would really be wired against a configured provider."""
        from datetime import UTC, date, datetime

        import services.scheduler as scheduler_module
        from models.earnings_calendar_event import EarningsCalendarEvent
        from models.enums import EarningsCalendarEventStatus
        from models.research_preparation_job import JobStatus, ResearchPreparationJob

        due_date = date(2034, 2, 1)
        now = datetime(2034, 1, 30, tzinfo=UTC)  # 2 days before due_date, inside the real
        # PREPARATION_LOOKAHEAD_DAYS(5) window -- see candidate_events_for_preparation.
        symbols = ["TESTPROGONE", "TESTPROGTWO", "TESTPROGTHREE"]
        events_by_symbol = {}
        for symbol in symbols:
            event = EarningsCalendarEvent(
                symbol=symbol,
                company_name=f"Test {symbol} Co",
                earnings_date=due_date,
                earnings_time=EarningsTiming.AMC,
                status=EarningsCalendarEventStatus.UPCOMING,
                market_cap=50_000_000_000,
                country="US",
            )
            db_session.add(event)
            db_session.flush()
            events_by_symbol[symbol] = event
        db_session.commit()

        monkeypatch.setattr(
            scheduler_module,
            "build_options_provider_chain",
            lambda settings, db=None: self._FakeOptionsProvider(),
        )

        results = scheduler_module.run_earnings_research_preparation_job(now=now)

        assert {r.symbol for r in results} == set(symbols)
        assert all(r.outcome == "queued" for r in results)

        run = the_scheduler_run(rollback_safe_session_local)
        assert run.status == "success"
        assert run.items_evaluated == 3
        assert run.items_succeeded == 3
        assert run.items_failed == 0
        events = the_scheduler_run_events(rollback_safe_session_local)
        assert {e.symbol for e in events} == set(symbols)
        assert all(e.stage == "preparation" and e.outcome == "queued" for e in events)

        for symbol in symbols:
            row = db_session.query(ResearchPreparationJob).filter_by(ticker=symbol).one()
            assert row.status == JobStatus.PENDING
            assert row.earnings_calendar_event_id == events_by_symbol[symbol].id

    def test_filtered_out_candidates_are_recorded_but_never_enqueued(
        self, monkeypatch, rollback_safe_session_local, db_session
    ):
        from datetime import UTC, date, datetime

        import services.scheduler as scheduler_module
        from models.earnings_calendar_event import EarningsCalendarEvent
        from models.enums import EarningsCalendarEventStatus
        from models.research_preparation_job import ResearchPreparationJob

        due_date = date(2034, 3, 1)
        now = datetime(2034, 2, 27, tzinfo=UTC)  # 2 days before due_date, inside the lookahead
        event = EarningsCalendarEvent(
            symbol="TESTPROGFILTERED",
            company_name="Test TESTPROGFILTERED Co",
            earnings_date=due_date,
            earnings_time=EarningsTiming.AMC,
            status=EarningsCalendarEventStatus.UPCOMING,
            market_cap=500_000_000,  # below the real cheap-filter threshold
            country="US",
        )
        db_session.add(event)
        db_session.commit()

        monkeypatch.setattr(
            scheduler_module,
            "build_options_provider_chain",
            lambda settings, db=None: self._FakeOptionsProvider(),
        )

        scheduler_module.run_earnings_research_preparation_job(now=now)

        run = the_scheduler_run(rollback_safe_session_local)
        assert run.status == "success"
        assert run.items_evaluated == 1
        assert run.items_succeeded == 1  # "filtered_out" is a real, non-error outcome
        assert run.items_failed == 0
        events = the_scheduler_run_events(rollback_safe_session_local)
        assert events[0].outcome == "filtered_out"
        assert (
            db_session.query(ResearchPreparationJob).filter_by(ticker="TESTPROGFILTERED").count()
            == 0
        )

    def test_a_genuine_error_marks_the_run_failed_and_returns_no_results(
        self, monkeypatch, rollback_safe_session_local
    ):
        import services.scheduler as scheduler_module

        def _raise(settings, db=None):
            raise RuntimeError("options provider misconfigured")

        monkeypatch.setattr(scheduler_module, "build_options_provider_chain", _raise)

        results = scheduler_module.run_earnings_research_preparation_job()

        assert results == []
        run = the_scheduler_run(rollback_safe_session_local)
        assert run.status == "error"






class TestGetSchedulerStatus:
    """Phase 4.9 -- GET /system-status's scheduler field. Driven through
    the real app + TestClient, not a bare build_scheduler(), because
    AsyncIOScheduler.start() needs a running asyncio event loop (see
    test_scheduler_starts_and_shuts_down_gracefully_with_the_app above
    for the same constraint)."""

    def test_none_scheduler_reports_not_running_honestly(self):
        # api/main.py's lifespan() sets app.state.scheduler = None when
        # startup itself failed -- this must be reported as real status,
        # never raise.
        status = get_scheduler_status(None)

        assert status.running is False
        assert status.jobs == []

    def test_real_scheduler_reports_running_and_all_jobs_with_next_run_time(self):
        import api.main as main_module

        app = main_module.create_app()
        with TestClient(app):
            status = get_scheduler_status(app.state.scheduler)

            assert status.running is True
            job_ids = {job.job_id for job in status.jobs}
            assert job_ids == _expected_registered_ids()
            # Every job has a real cron/interval trigger -- next_run_time
            # must always be populated once the scheduler is running,
            # never left None/unknown.
            assert all(job.next_run_time is not None for job in status.jobs)

    def test_stopped_scheduler_reports_running_false(self):
        import api.main as main_module

        app = main_module.create_app()
        with TestClient(app):
            scheduler = app.state.scheduler

        # Outside the `with` block -- TestClient's __exit__ has already
        # driven the app's shutdown, which shuts the scheduler down too.
        status = get_scheduler_status(scheduler)

        assert status.running is False

    def test_job_execution_updates_last_run_at_and_status(self):
        import api.main as main_module

        app = main_module.create_app()
        with TestClient(app):
            scheduler = app.state.scheduler
            before = get_scheduler_status(scheduler)
            assert before.jobs[0].last_run_at is None  # nothing has run yet

            event = JobExecutionEvent(
                code=EVENT_JOB_EXECUTED,
                job_id=CALENDAR_SYNC_JOB_ID,
                jobstore="default",
                scheduled_run_time=None,
            )
            # The real, documented way APScheduler itself uses to invoke
            # its listeners -- there is no public equivalent, and waiting
            # for a real cron/interval trigger to fire isn't practical in
            # a unit test.
            scheduler._dispatch_event(event)  # noqa: SLF001

            after = get_scheduler_status(scheduler)
            calendar_job = next(j for j in after.jobs if j.job_id == CALENDAR_SYNC_JOB_ID)
            assert calendar_job.last_run_at is not None
            assert calendar_job.last_run_status == "success"

    def test_job_execution_error_recorded_as_error_status(self):
        import api.main as main_module

        app = main_module.create_app()
        with TestClient(app):
            scheduler = app.state.scheduler
            event = JobExecutionEvent(
                code=EVENT_JOB_ERROR,
                job_id=CALENDAR_SYNC_JOB_ID,
                jobstore="default",
                scheduled_run_time=None,
                exception=RuntimeError("boom"),
            )
            scheduler._dispatch_event(event)  # noqa: SLF001

            status = get_scheduler_status(scheduler)
            sync_job = next(j for j in status.jobs if j.job_id == CALENDAR_SYNC_JOB_ID)
            assert sync_job.last_run_status == "error"


