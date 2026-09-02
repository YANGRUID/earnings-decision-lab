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

from analytics.earnings_timing import compute_entry_exit_schedule
from core.config import Settings
from models.enums import AnnouncementTime, EarningsTiming
from models.scheduler_run import SchedulerRun, SchedulerRunEvent
from services.scheduler import (
    CALENDAR_SYNC_JOB_ID,
    DECISION_AND_ENTRY_CAPTURE_JOB_ID,
    EARNINGS_RESEARCH_PREPARATION_JOB_ID,
    EXIT_CAPTURE_JOB_ID,
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

def _due_now_for(earnings_date, earnings_time):
    """The real, authoritative entry_timestamp for an event with this
    earnings_date/earnings_time -- pre-live hardening added a real
    server-side due-window filter to run_decision_and_entry_capture_job
    (see services/scheduler.py's own _due_for_decision_now), so a test
    event with a fixed, far-future earnings_date now also needs a
    matching, explicit ``now`` for the job to ever consider it due.
    Computed via the exact same authoritative function the job itself
    uses, never a guessed/hardcoded timestamp."""
    session = _TIMING_TO_ANNOUNCEMENT[earnings_time]
    return compute_entry_exit_schedule(earnings_date, session).entry_timestamp


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


def test_job_persists_across_app_restart_without_duplicating():
    """SQLAlchemyJobStore persists the job registrations to the database;
    a second, independent app instance (simulating a process restart)
    picks up the persisted jobs, and replace_existing=True means
    re-registering them doesn't create competing duplicates -- the real
    mechanism behind "job survives restart" / "restarting the service
    must not create duplicate official entry snapshots" (Phase 4.4 sec
    14/15 -- this is the scheduler-level half of that guarantee; the
    entry-capture service's own idempotency, tested separately in
    test_services_benchmark_entry_capture.py, is the other half)."""
    import api.main as main_module

    app1 = main_module.create_app()
    with TestClient(app1):
        assert len(app1.state.scheduler.get_jobs()) == len(_expected_registered_ids())

    app2 = main_module.create_app()
    with TestClient(app2):
        jobs = app2.state.scheduler.get_jobs()
        assert len(jobs) == len(_expected_registered_ids())
        assert app2.state.scheduler.get_job(CALENDAR_SYNC_JOB_ID) is not None
        assert app2.state.scheduler.get_job(EARNINGS_RESEARCH_PREPARATION_JOB_ID) is not None
        assert app2.state.scheduler.get_job(DECISION_AND_ENTRY_CAPTURE_JOB_ID) is None  # V3 retired
        assert app2.state.scheduler.get_job(EXIT_CAPTURE_JOB_ID) is None
        assert app2.state.scheduler.get_job(IBKR_GATEWAY_HEALTHCHECK_JOB_ID) is not None


def test_scheduler_starts_and_shuts_down_gracefully_with_the_app():
    import api.main as main_module

    app = main_module.create_app()
    with TestClient(app):
        assert app.state.scheduler is not None
        assert app.state.scheduler.running is True
        assert app.state.scheduler.get_job(CALENDAR_SYNC_JOB_ID) is not None
        assert app.state.scheduler.get_job(DECISION_AND_ENTRY_CAPTURE_JOB_ID) is None  # V3 retired
        assert app.state.scheduler.get_job(EXIT_CAPTURE_JOB_ID) is None
        assert app.state.scheduler.get_job(IBKR_GATEWAY_HEALTHCHECK_JOB_ID) is not None

    assert app.state.scheduler.running is False


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


class TestDecisionAndEntryCaptureJobTracking:
    """Operations Monitor tracking for the real per-event decision +
    entry loop. The underlying pipeline functions (run_decision_
    pipeline_for_event, capture_benchmark_entry) already have their own
    full test coverage elsewhere (test_services_decision_pipeline.py,
    test_services_benchmark_entry_capture.py) -- these tests monkeypatch
    those two functions directly to verify only the new instrumentation
    records real, correct SchedulerRunEvent rows, without needing a real
    LLM/embedder/options-provider stack."""

    def test_records_one_event_per_stage_actually_reached(
        self, monkeypatch, rollback_safe_session_local, db_session
    ):
        from datetime import date

        import services.scheduler as scheduler_module
        from models.earnings_calendar_event import EarningsCalendarEvent
        from models.entry_capture_attempt import EntryCaptureAttempt
        from models.enums import CaptureStatus, EarningsCalendarEventStatus, EarningsTiming
        from services.decision_pipeline import DecisionPipelineOutcome

        event = EarningsCalendarEvent(
            symbol="TESTOPS",
            company_name="Test Operations Co",
            earnings_date=date(2030, 1, 5),
            earnings_time=EarningsTiming.AMC,
            status=EarningsCalendarEventStatus.UPCOMING,
        )
        db_session.add(event)
        db_session.flush()

        monkeypatch.setattr(scheduler_module, "_shared_embedder", object())
        monkeypatch.setattr(
            scheduler_module, "get_llm_provider", lambda settings, db=None: object()
        )
        monkeypatch.setattr(
            scheduler_module, "build_options_provider_chain", lambda settings, db=None: object()
        )

        # The job's own due-window pre-filter (services/scheduler.py's
        # _due_for_decision_now) means TESTOPS's fixed ``now`` above
        # should already be the only candidate row in scope, but the
        # fake pipeline still handles "any other row" defensively --
        # edl-test-db is shared with the Playwright E2E suite and other
        # pytest runs (see conftest.py), so a harmless, honest
        # "skipped_not_due" for anything unexpected costs nothing and
        # never touches or mutates it.
        def fake_pipeline(db, ev, portfolio, options_provider, llm, embedder, now=None):
            if ev.symbol == "TESTOPS":
                return DecisionPipelineOutcome(
                    ev.id, ev.symbol, "created", decision_snapshot_id=999
                )
            return DecisionPipelineOutcome(ev.id, ev.symbol, "skipped_not_due")

        monkeypatch.setattr(scheduler_module, "run_decision_pipeline_for_event", fake_pipeline)

        fake_attempt = EntryCaptureAttempt(
            decision_snapshot_id=999, benchmark_portfolio_id=1, status=CaptureStatus.CAPTURED
        )
        monkeypatch.setattr(
            scheduler_module,
            "capture_benchmark_entry",
            lambda db, **kw: fake_attempt,
        )
        # decision_snapshot_id=999 doesn't exist -- db.get() returns None,
        # so capture_benchmark_entry is never actually called; this test
        # only needs the "decision" stage event to be recorded. The entry
        # stage's own SchedulerRunEvent recording is covered directly by
        # test_records_an_entry_capture_outcome below, against a real
        # DecisionSnapshot.
        scheduler_module.run_decision_and_entry_capture_job(
            now=_due_now_for(date(2030, 1, 5), EarningsTiming.AMC)
        )

        events = the_scheduler_run_events(rollback_safe_session_local, symbol="TESTOPS")
        assert len(events) == 1
        assert events[0].stage == "decision"
        assert events[0].outcome == "created"

        run = the_scheduler_run(rollback_safe_session_local)
        assert run.job_id == DECISION_AND_ENTRY_CAPTURE_JOB_ID
        assert run.status == "success"
        assert run.items_failed == 0  # nothing genuinely failed, real or fake

    def test_records_an_entry_capture_outcome(
        self, monkeypatch, rollback_safe_session_local, db_session
    ):
        """A real DecisionSnapshot/BenchmarkPortfolio (matching test_
        services_benchmark_entry_capture.py's own fixture pattern) so
        db.get(DecisionSnapshot, ...) inside the job body succeeds and
        the entry stage is actually reached -- capture_benchmark_entry
        itself is monkeypatched (its own real behavior already has full
        coverage in that other file); this test is only about the new
        SchedulerRunEvent recorded around it."""
        from datetime import UTC, date, datetime
        from decimal import Decimal

        import services.scheduler as scheduler_module
        from models.benchmark_portfolio import BenchmarkPortfolio
        from models.decision_snapshot import DecisionSnapshot
        from models.earnings_calendar_event import EarningsCalendarEvent
        from models.entry_capture_attempt import EntryCaptureAttempt
        from models.enums import (
            CaptureStatus,
            DecisionDirection,
            DecisionSnapshotStatus,
            EarningsCalendarEventStatus,
            EarningsTiming,
            RiskProfile,
        )
        from services.decision_pipeline import DecisionPipelineOutcome

        event = EarningsCalendarEvent(
            symbol="TESTENTRY",
            company_name="Test Entry Co",
            earnings_date=date(2030, 1, 5),
            earnings_time=EarningsTiming.AMC,
            status=EarningsCalendarEventStatus.UPCOMING,
        )
        portfolio = BenchmarkPortfolio(
            name="Test Entry Portfolio",
            initial_capital=Decimal("2000.00"),
            cash_balance=Decimal("2000.00"),
            risk_profile=RiskProfile.MODERATE,
        )
        db_session.add_all([event, portfolio])
        db_session.flush()
        decision = DecisionSnapshot(
            earnings_calendar_event_id=event.id,
            benchmark_portfolio_id=portfolio.id,
            ticker=event.symbol,
            company_name=event.company_name,
            strategy_direction=DecisionDirection.BULLISH,
            strategy_type="long_call",
            generated_at=datetime.now(UTC),
            status=DecisionSnapshotStatus.PENDING_ENTRY,
            legs=[{"option_type": "call", "action": "buy", "strike": "100", "quantity": 1}],
            engine_version="v3",
            prompt_version="v1",
            expiration_source="v3_auto_resolver",
        )
        db_session.add(decision)
        db_session.flush()

        monkeypatch.setattr(scheduler_module, "_shared_embedder", object())
        monkeypatch.setattr(
            scheduler_module, "get_llm_provider", lambda settings, db=None: object()
        )
        monkeypatch.setattr(
            scheduler_module, "build_options_provider_chain", lambda settings, db=None: object()
        )

        # Same real-row-safety rationale as test_records_one_event_per_
        # stage_actually_reached above: only TESTENTRY gets a real
        # decision_snapshot_id (so only it reaches the entry-capture
        # call); every other real row gets a harmless, non-failing skip.
        def fake_pipeline(db, ev, portfolio, options_provider, llm, embedder, now=None):
            if ev.symbol == "TESTENTRY":
                return DecisionPipelineOutcome(
                    ev.id, ev.symbol, "created", decision_snapshot_id=decision.id
                )
            return DecisionPipelineOutcome(ev.id, ev.symbol, "skipped_not_due")

        monkeypatch.setattr(scheduler_module, "run_decision_pipeline_for_event", fake_pipeline)

        fake_attempt = EntryCaptureAttempt(
            decision_snapshot_id=decision.id,
            benchmark_portfolio_id=portfolio.id,
            status=CaptureStatus.FAILED,
            capture_error="no usable option quotes",
        )
        monkeypatch.setattr(
            scheduler_module, "capture_benchmark_entry", lambda db, **kw: fake_attempt
        )

        scheduler_module.run_decision_and_entry_capture_job(
            now=_due_now_for(date(2030, 1, 5), EarningsTiming.AMC)
        )

        entry_events = the_scheduler_run_events(rollback_safe_session_local, stage="entry")
        assert len(entry_events) == 1
        assert entry_events[0].symbol == "TESTENTRY"
        assert entry_events[0].outcome == "entry_failed"
        assert entry_events[0].reason == "no usable option quotes"

        run = the_scheduler_run(rollback_safe_session_local)
        # TESTENTRY's own entry-stage FAILED outcome counts toward the
        # run's own items_failed total; every other real row got a
        # harmless, non-failing skip and contributes nothing to it.
        assert run.items_failed == 1

    def test_no_action_entry_capture_never_counts_as_items_failed(
        self, monkeypatch, rollback_safe_session_local, db_session
    ):
        """Post-official-run cleanup (2026-08-27), Section 1/2 -- the real
        Aug 26 bug: a decision with no recommended strategy legs (the
        real A/P shape) gets a FAILED EntryCaptureAttempt from
        capture_benchmark_entry (correctly -- there is nothing to
        capture), but that is a real, successful pipeline evaluation, not
        an infrastructure failure, and must never increment items_failed
        or be recorded as a "failed" scheduler_run_event."""
        from datetime import UTC, date, datetime
        from decimal import Decimal

        import services.scheduler as scheduler_module
        from models.benchmark_portfolio import BenchmarkPortfolio
        from models.decision_snapshot import DecisionSnapshot
        from models.earnings_calendar_event import EarningsCalendarEvent
        from models.entry_capture_attempt import EntryCaptureAttempt
        from models.enums import (
            CaptureStatus,
            DecisionDirection,
            DecisionSnapshotStatus,
            EarningsCalendarEventStatus,
            EarningsTiming,
            RiskProfile,
        )
        from services.decision_pipeline import DecisionPipelineOutcome

        event = EarningsCalendarEvent(
            symbol="TESTNOACT",
            company_name="Test No-Action Co",
            earnings_date=date(2030, 1, 5),
            earnings_time=EarningsTiming.AMC,
            status=EarningsCalendarEventStatus.UPCOMING,
        )
        portfolio = BenchmarkPortfolio(
            name="Test No-Action Portfolio",
            initial_capital=Decimal("2000.00"),
            cash_balance=Decimal("2000.00"),
            risk_profile=RiskProfile.MODERATE,
        )
        db_session.add_all([event, portfolio])
        db_session.flush()
        # legs=None -- the real A/P shape: the strategy engine found no
        # actionable strategy for this event.
        decision = DecisionSnapshot(
            earnings_calendar_event_id=event.id,
            benchmark_portfolio_id=portfolio.id,
            ticker=event.symbol,
            company_name=event.company_name,
            strategy_direction=DecisionDirection.NEUTRAL,
            generated_at=datetime.now(UTC),
            status=DecisionSnapshotStatus.PENDING_ENTRY,
            legs=None,
            engine_version="v3",
            prompt_version="v1",
            expiration_source="v3_auto_resolver",
        )
        db_session.add(decision)
        db_session.flush()

        monkeypatch.setattr(scheduler_module, "_shared_embedder", object())
        monkeypatch.setattr(
            scheduler_module, "get_llm_provider", lambda settings, db=None: object()
        )
        monkeypatch.setattr(
            scheduler_module, "build_options_provider_chain", lambda settings, db=None: object()
        )

        def fake_pipeline(db, ev, portfolio, options_provider, llm, embedder, now=None):
            if ev.symbol == "TESTNOACT":
                return DecisionPipelineOutcome(
                    ev.id, ev.symbol, "created", decision_snapshot_id=decision.id
                )
            return DecisionPipelineOutcome(ev.id, ev.symbol, "skipped_not_due")

        monkeypatch.setattr(scheduler_module, "run_decision_pipeline_for_event", fake_pipeline)

        # The real capture_benchmark_entry behavior for a no-legs decision
        # (services/benchmark_entry_capture.py) -- a FAILED attempt whose
        # capture_error is the real, stable "no recommended strategy legs"
        # message, never CAPTURED.
        fake_attempt = EntryCaptureAttempt(
            decision_snapshot_id=decision.id,
            benchmark_portfolio_id=portfolio.id,
            status=CaptureStatus.FAILED,
            capture_error="decision_snapshot has no recommended strategy legs to enter",
        )
        monkeypatch.setattr(
            scheduler_module, "capture_benchmark_entry", lambda db, **kw: fake_attempt
        )

        scheduler_module.run_decision_and_entry_capture_job(
            now=_due_now_for(date(2030, 1, 5), EarningsTiming.AMC)
        )

        entry_events = the_scheduler_run_events(rollback_safe_session_local, stage="entry")
        assert len(entry_events) == 1
        assert entry_events[0].symbol == "TESTNOACT"
        assert entry_events[0].outcome == "decision_no_action"

        run = the_scheduler_run(rollback_safe_session_local)
        assert run.items_failed == 0

    def test_records_a_decision_pipeline_exception_as_failed(
        self, monkeypatch, rollback_safe_session_local, db_session
    ):
        from datetime import date

        import services.scheduler as scheduler_module
        from models.earnings_calendar_event import EarningsCalendarEvent
        from models.enums import EarningsCalendarEventStatus, EarningsTiming
        from services.decision_pipeline import DecisionPipelineOutcome

        event = EarningsCalendarEvent(
            symbol="TESTFAIL",
            company_name="Test Failing Co",
            earnings_date=date(2030, 1, 5),
            earnings_time=EarningsTiming.AMC,
            status=EarningsCalendarEventStatus.UPCOMING,
        )
        db_session.add(event)
        db_session.flush()

        monkeypatch.setattr(scheduler_module, "_shared_embedder", object())
        monkeypatch.setattr(
            scheduler_module, "get_llm_provider", lambda settings, db=None: object()
        )
        monkeypatch.setattr(
            scheduler_module, "build_options_provider_chain", lambda settings, db=None: object()
        )

        # Only TESTFAIL crashes -- every other real UPCOMING row (this
        # shared dev Postgres instance already has thousands) gets a
        # harmless, non-failing skip, matching the same real-row-safety
        # rationale as the other tests in this class.
        def maybe_raise(db, ev, portfolio, options_provider, llm, embedder, now=None):
            if ev.symbol == "TESTFAIL":
                raise RuntimeError("simulated pipeline crash")
            return DecisionPipelineOutcome(ev.id, ev.symbol, "skipped_not_due")

        monkeypatch.setattr(scheduler_module, "run_decision_pipeline_for_event", maybe_raise)

        scheduler_module.run_decision_and_entry_capture_job(
            now=_due_now_for(date(2030, 1, 5), EarningsTiming.AMC)
        )

        (run_event,) = the_scheduler_run_events(rollback_safe_session_local, symbol="TESTFAIL")
        assert run_event.stage == "decision"
        assert run_event.outcome == "failed"
        assert "simulated pipeline crash" in run_event.reason

        run = the_scheduler_run(rollback_safe_session_local)
        assert run.status == "success"  # the run itself completed; one item failed
        assert run.items_failed == 1

    def test_setup_failure_still_raises_after_recording(
        self, monkeypatch, rollback_safe_session_local
    ):
        """The one real behavior this instrumentation must never change:
        a setup-stage exception (before the per-event loop even starts)
        still propagates to APScheduler's own EVENT_JOB_ERROR listener,
        exactly as it did before scheduler_run tracking existed --
        get_scheduler_status()'s existing last_run_status must never
        start silently reporting "success" for a run that actually
        failed at setup."""
        import services.scheduler as scheduler_module

        monkeypatch.setattr(scheduler_module, "_shared_embedder", object())

        def _raise(settings, db=None):
            raise RuntimeError("simulated LLM provider construction failure")

        monkeypatch.setattr(scheduler_module, "get_llm_provider", _raise)

        with pytest.raises(RuntimeError, match="simulated LLM provider construction failure"):
            scheduler_module.run_decision_and_entry_capture_job()

        run = the_scheduler_run(rollback_safe_session_local)
        assert run.status == "error"
        assert "simulated LLM provider construction failure" in run.error_summary


class TestDecisionCandidateWindowing:
    """Pre-live hardening (2026-08-25): run_decision_and_entry_capture_job
    must not spend the real 15:55 ET window walking every UPCOMING event
    -- see services/scheduler.py's own _due_for_decision_now docstring
    for the live-observed problem this fixes (every non-due event used
    to reach a real, expensive check_eligibility() call). These tests
    verify the orchestration-level filter directly: run_decision_
    pipeline_for_event (monkeypatched to a call-recording spy, never the
    real pipeline) must only ever be invoked for events genuinely due at
    the fixed ``now`` passed in, never for ones weeks/months away."""

    def test_far_future_and_far_past_events_never_reach_the_pipeline(
        self, monkeypatch, rollback_safe_session_local, db_session
    ):
        from datetime import date

        import services.scheduler as scheduler_module
        from models.earnings_calendar_event import EarningsCalendarEvent
        from models.enums import EarningsCalendarEventStatus

        due_date = date(2031, 6, 10)
        due_now = _due_now_for(due_date, EarningsTiming.AMC)

        due_event = EarningsCalendarEvent(
            symbol="TESTDUE",
            company_name="Test Due Co",
            earnings_date=due_date,
            earnings_time=EarningsTiming.AMC,
            status=EarningsCalendarEventStatus.UPCOMING,
        )
        far_future_event = EarningsCalendarEvent(
            symbol="TESTFARFUTURE",
            company_name="Test Far Future Co",
            earnings_date=due_date + timedelta(days=90),
            earnings_time=EarningsTiming.AMC,
            status=EarningsCalendarEventStatus.UPCOMING,
        )
        far_past_event = EarningsCalendarEvent(
            symbol="TESTFARPAST",
            company_name="Test Far Past Co",
            earnings_date=due_date - timedelta(days=90),
            earnings_time=EarningsTiming.AMC,
            status=EarningsCalendarEventStatus.UPCOMING,
        )
        db_session.add_all([due_event, far_future_event, far_past_event])
        db_session.flush()

        monkeypatch.setattr(scheduler_module, "_shared_embedder", object())
        monkeypatch.setattr(
            scheduler_module, "get_llm_provider", lambda settings, db=None: object()
        )
        monkeypatch.setattr(
            scheduler_module, "build_options_provider_chain", lambda settings, db=None: object()
        )

        seen_symbols: list[str] = []

        def spy_pipeline(db, ev, portfolio, options_provider, llm, embedder, now=None):
            from services.decision_pipeline import DecisionPipelineOutcome

            seen_symbols.append(ev.symbol)
            return DecisionPipelineOutcome(ev.id, ev.symbol, "skipped_not_due")

        monkeypatch.setattr(scheduler_module, "run_decision_pipeline_for_event", spy_pipeline)

        scheduler_module.run_decision_and_entry_capture_job(now=due_now)

        # The far-future and far-past rows must never even reach the real
        # pipeline function -- not "reach it and get skipped inside", an
        # orchestration-level exclusion before that call happens at all.
        assert seen_symbols == ["TESTDUE"]

    def test_a_genuinely_due_event_is_processed(
        self, monkeypatch, rollback_safe_session_local, db_session
    ):
        from datetime import date

        import services.scheduler as scheduler_module
        from models.earnings_calendar_event import EarningsCalendarEvent
        from models.enums import EarningsCalendarEventStatus

        due_date = date(2031, 9, 2)
        due_now = _due_now_for(due_date, EarningsTiming.BMO)

        event = EarningsCalendarEvent(
            symbol="TESTDUEBMO",
            company_name="Test Due BMO Co",
            earnings_date=due_date,
            earnings_time=EarningsTiming.BMO,
            status=EarningsCalendarEventStatus.UPCOMING,
        )
        db_session.add(event)
        db_session.flush()

        monkeypatch.setattr(scheduler_module, "_shared_embedder", object())
        monkeypatch.setattr(
            scheduler_module, "get_llm_provider", lambda settings, db=None: object()
        )
        monkeypatch.setattr(
            scheduler_module, "build_options_provider_chain", lambda settings, db=None: object()
        )

        seen_symbols: list[str] = []

        def spy_pipeline(db, ev, portfolio, options_provider, llm, embedder, now=None):
            from services.decision_pipeline import DecisionPipelineOutcome

            seen_symbols.append(ev.symbol)
            return DecisionPipelineOutcome(ev.id, ev.symbol, "skipped_not_due")

        monkeypatch.setattr(scheduler_module, "run_decision_pipeline_for_event", spy_pipeline)

        scheduler_module.run_decision_and_entry_capture_job(now=due_now)

        assert seen_symbols == ["TESTDUEBMO"]

    def test_late_cutoff_grace_boundary_is_respected(
        self, monkeypatch, rollback_safe_session_local, db_session
    ):
        """A few minutes past LATE_CUTOFF_GRACE, the event must be
        excluded -- the same authoritative boundary run_decision_
        pipeline_for_event itself already enforces, reused here rather
        than redefined, so the two can never disagree."""
        from datetime import date

        import services.scheduler as scheduler_module
        from models.earnings_calendar_event import EarningsCalendarEvent
        from models.enums import EarningsCalendarEventStatus
        from services.decision_pipeline import LATE_CUTOFF_GRACE

        due_date = date(2031, 11, 3)
        entry_timestamp = _due_now_for(due_date, EarningsTiming.AMC)
        too_late_now = entry_timestamp + LATE_CUTOFF_GRACE + timedelta(minutes=1)

        event = EarningsCalendarEvent(
            symbol="TESTTOOLATE",
            company_name="Test Too Late Co",
            earnings_date=due_date,
            earnings_time=EarningsTiming.AMC,
            status=EarningsCalendarEventStatus.UPCOMING,
        )
        db_session.add(event)
        db_session.flush()

        monkeypatch.setattr(scheduler_module, "_shared_embedder", object())
        monkeypatch.setattr(
            scheduler_module, "get_llm_provider", lambda settings, db=None: object()
        )
        monkeypatch.setattr(
            scheduler_module, "build_options_provider_chain", lambda settings, db=None: object()
        )

        seen_symbols: list[str] = []

        def spy_pipeline(db, ev, portfolio, options_provider, llm, embedder, now=None):
            from services.decision_pipeline import DecisionPipelineOutcome

            seen_symbols.append(ev.symbol)
            return DecisionPipelineOutcome(ev.id, ev.symbol, "skipped_not_due")

        monkeypatch.setattr(scheduler_module, "run_decision_pipeline_for_event", spy_pipeline)

        scheduler_module.run_decision_and_entry_capture_job(now=too_late_now)

        assert seen_symbols == []


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


class TestMultiCompanyThroughput:
    """Pre-live hardening (2026-08-25) Section 5: proves 5 simultaneously-
    due companies can be sequentially orchestrated (eligibility -> decision
    -> entry) within the real 5-minute LATE_CUTOFF_GRACE window, using
    safe, monkeypatched fixtures -- never a real LLM/IBKR call, never an
    official DecisionSnapshot/EntrySnapshot.

    This measures ORCHESTRATION overhead only (the Python loop itself:
    per-event DB queries/commits, SchedulerRunEvent bookkeeping) -- real
    per-company work is stood in for with a small, explicitly-labeled
    illustrative delay (conftest.py's autouse _no_real_sleeps fixture is
    deliberately bypassed for this one test, restoring real time.sleep,
    so the delay is real wall-clock time, not faked away). See this
    test's own assertion message for how the measured orchestration
    overhead is combined with a realistic real-world per-company latency
    assumption to reach the throughput conclusion reported to the user.
    """

    # Deliberately small stand-ins for real per-company work (a real LLM
    # generation call + a real options-quote fetch), not a claim that
    # real work takes exactly this long -- see the module docstring
    # above. Scaled down so this test itself stays fast; the report this
    # test's result feeds into separately reasons about realistic
    # real-world latency (this project's own real DeepSeek/IBKR calls
    # observed elsewhere this session run a few seconds each).
    _SIMULATED_DECISION_LATENCY_SECONDS = 0.15
    _SIMULATED_ENTRY_LATENCY_SECONDS = 0.1

    def test_five_simultaneously_due_companies_sequential_orchestration(
        self, monkeypatch, rollback_safe_session_local, db_session
    ):
        import time as time_module
        from datetime import date
        from decimal import Decimal

        import services.scheduler as scheduler_module
        from models.benchmark_portfolio import BenchmarkPortfolio
        from models.decision_snapshot import DecisionSnapshot
        from models.earnings_calendar_event import EarningsCalendarEvent
        from models.entry_capture_attempt import EntryCaptureAttempt
        from models.enums import (
            CaptureStatus,
            DecisionDirection,
            DecisionSnapshotStatus,
            EarningsCalendarEventStatus,
        )
        from services.decision_pipeline import DecisionPipelineOutcome

        # conftest.py's autouse _no_real_sleeps fixture patches time.sleep
        # to a no-op globally -- correct for every other test (nothing
        # should ever wait on a real clock), wrong for exactly this one,
        # which needs to measure real elapsed wall-clock time across a
        # deliberately real (if small) simulated delay. Restore the
        # genuine sleep (captured at module-import time, before that
        # fixture ever ran) for the duration of this test only.
        monkeypatch.setattr(time_module, "sleep", _REAL_SLEEP)
        real_sleep = _REAL_SLEEP

        due_date = date(2032, 3, 10)
        due_now = _due_now_for(due_date, EarningsTiming.AMC)

        portfolio = BenchmarkPortfolio(
            name="Test Throughput Portfolio",
            initial_capital=Decimal("50000.00"),
            cash_balance=Decimal("50000.00"),
            risk_profile="moderate",
            is_active=True,
        )
        db_session.add(portfolio)
        db_session.flush()

        symbols = ["TESTCO1", "TESTCO2", "TESTCO3", "TESTCO4", "TESTCO5"]
        decisions_by_symbol: dict[str, int] = {}
        for symbol in symbols:
            event = EarningsCalendarEvent(
                symbol=symbol,
                company_name=f"Test Throughput {symbol}",
                earnings_date=due_date,
                earnings_time=EarningsTiming.AMC,
                status=EarningsCalendarEventStatus.UPCOMING,
            )
            db_session.add(event)
            db_session.flush()
            decision = DecisionSnapshot(
                earnings_calendar_event_id=event.id,
                benchmark_portfolio_id=portfolio.id,
                ticker=symbol,
                company_name=event.company_name,
                strategy_direction=DecisionDirection.BULLISH,
                strategy_type="long_call",
                generated_at=due_now,
                status=DecisionSnapshotStatus.PENDING_ENTRY,
                legs=[{"option_type": "call", "action": "buy", "strike": "100", "quantity": 1}],
                engine_version="v3",
                prompt_version="v1",
                expiration_source="v3_auto_resolver",
            )
            db_session.add(decision)
            db_session.flush()
            decisions_by_symbol[symbol] = decision.id

        monkeypatch.setattr(scheduler_module, "_shared_embedder", object())
        monkeypatch.setattr(
            scheduler_module, "get_llm_provider", lambda settings, db=None: object()
        )
        monkeypatch.setattr(
            scheduler_module, "build_options_provider_chain", lambda settings, db=None: object()
        )

        def spy_pipeline(db, ev, portfolio, options_provider, llm, embedder, now=None):
            real_sleep(self._SIMULATED_DECISION_LATENCY_SECONDS)
            return DecisionPipelineOutcome(
                ev.id, ev.symbol, "created", decision_snapshot_id=decisions_by_symbol[ev.symbol]
            )

        def spy_capture_entry(db, *, decision_snapshot, portfolio, options_provider, now):
            real_sleep(self._SIMULATED_ENTRY_LATENCY_SECONDS)
            return EntryCaptureAttempt(
                decision_snapshot_id=decision_snapshot.id,
                benchmark_portfolio_id=portfolio.id,
                status=CaptureStatus.CAPTURED,
            )

        monkeypatch.setattr(scheduler_module, "run_decision_pipeline_for_event", spy_pipeline)
        monkeypatch.setattr(scheduler_module, "capture_benchmark_entry", spy_capture_entry)

        started = time_module.perf_counter()
        scheduler_module.run_decision_and_entry_capture_job(now=due_now)
        elapsed_seconds = time_module.perf_counter() - started

        run = the_scheduler_run(rollback_safe_session_local)
        assert run.status == "success"
        assert run.items_evaluated == 5
        assert run.items_failed == 0

        entry_events = the_scheduler_run_events(rollback_safe_session_local, stage="entry")
        assert {e.symbol for e in entry_events} == set(symbols)
        assert all(e.outcome == "entry_captured" for e in entry_events)

        # 5 sequential companies at the simulated per-company latency
        # above must land comfortably inside real orchestration overhead
        # + simulated work -- a generous ceiling (not a tight benchmark)
        # that would fail loudly if the loop were doing something
        # unexpectedly expensive per event (an accidental N+1 query,
        # for instance), not just to check it merely finishes.
        budget = (
            5 * (self._SIMULATED_DECISION_LATENCY_SECONDS + self._SIMULATED_ENTRY_LATENCY_SECONDS)
            + 2.0
        )
        assert elapsed_seconds < budget, (
            f"5-company sequential orchestration took {elapsed_seconds:.2f}s, "
            f"expected under {budget:.2f}s (simulated work + 2s orchestration budget)"
        )
