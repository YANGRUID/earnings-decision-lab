"""Phase 4.2/4.4/4.5/4.8A/4.9 -- tests for services/scheduler.py: all
four jobs are registered correctly, api/main.py's real lifespan
starts/stops them gracefully, and GET /system-status's scheduler field
(Phase 4.9) reflects real, live scheduler state -- never assumed."""

import logging

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED, JobExecutionEvent
from fastapi.testclient import TestClient

from core.config import Settings
from services.scheduler import (
    CALENDAR_SYNC_JOB_ID,
    DECISION_AND_ENTRY_CAPTURE_JOB_ID,
    EXIT_CAPTURE_JOB_ID,
    IBKR_GATEWAY_HEALTHCHECK_JOB_ID,
    build_scheduler,
    get_scheduler_status,
    run_ibkr_gateway_healthcheck_job,
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


def test_decision_and_entry_capture_job_registered_correctly():
    """Phase 4.4 sec 14: one daily cron job at the fixed 15:55 ET entry
    time (analytics/earnings_timing.py's own ENTRY_EXIT_TIME), in
    America/New_York -- not the scheduler's default UTC, and not a
    continuously-polling job (every eligible event's entry_timestamp
    resolves to this same wall-clock time, only the date varies)."""
    scheduler = build_scheduler()

    job = scheduler.get_job(DECISION_AND_ENTRY_CAPTURE_JOB_ID)
    assert job is not None
    assert job.func.__name__ == "run_decision_and_entry_capture_job"
    assert job.args == ()  # not a job argument -- see the module's own note on pickling

    field_by_name = {field.name: field for field in job.trigger.fields}
    assert str(field_by_name["hour"]) == "15"
    assert str(field_by_name["minute"]) == "55"
    assert str(job.trigger.timezone) == "America/New_York"


def test_exit_capture_job_registered_correctly():
    """Phase 4.5 approved decision 5: its own job, at the same 15:55 ET
    daily trigger the entry job uses (compute_entry_exit_schedule's
    exit_timestamp shares ENTRY_EXIT_TIME with entry_timestamp -- only
    the date differs per event)."""
    scheduler = build_scheduler()

    job = scheduler.get_job(EXIT_CAPTURE_JOB_ID)
    assert job is not None
    assert job.func.__name__ == "run_exit_capture_job"
    assert job.args == ()

    field_by_name = {field.name: field for field in job.trigger.fields}
    assert str(field_by_name["hour"]) == "15"
    assert str(field_by_name["minute"]) == "55"
    assert str(job.trigger.timezone) == "America/New_York"


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
        assert len(app1.state.scheduler.get_jobs()) == 4

    app2 = main_module.create_app()
    with TestClient(app2):
        jobs = app2.state.scheduler.get_jobs()
        assert len(jobs) == 4
        assert app2.state.scheduler.get_job(CALENDAR_SYNC_JOB_ID) is not None
        assert app2.state.scheduler.get_job(DECISION_AND_ENTRY_CAPTURE_JOB_ID) is not None
        assert app2.state.scheduler.get_job(EXIT_CAPTURE_JOB_ID) is not None
        assert app2.state.scheduler.get_job(IBKR_GATEWAY_HEALTHCHECK_JOB_ID) is not None


def test_scheduler_starts_and_shuts_down_gracefully_with_the_app():
    import api.main as main_module

    app = main_module.create_app()
    with TestClient(app):
        assert app.state.scheduler is not None
        assert app.state.scheduler.running is True
        assert app.state.scheduler.get_job(CALENDAR_SYNC_JOB_ID) is not None
        assert app.state.scheduler.get_job(DECISION_AND_ENTRY_CAPTURE_JOB_ID) is not None
        assert app.state.scheduler.get_job(EXIT_CAPTURE_JOB_ID) is not None
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

    def test_skips_when_options_provider_is_not_ibkr(self, monkeypatch, caplog):
        import services.scheduler as scheduler_module

        monkeypatch.setattr(
            scheduler_module,
            "get_settings",
            lambda: self._settings(options_provider="alpha_vantage"),
        )
        with caplog.at_level(logging.DEBUG, logger="services.scheduler"):
            run_ibkr_gateway_healthcheck_job()

        assert "skipped" in caplog.text

    def test_logs_info_when_connected(self, monkeypatch, httpx_mock, caplog):
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

    def test_logs_warning_when_not_authenticated(self, monkeypatch, httpx_mock, caplog):
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

    def test_logs_warning_when_gateway_unreachable(self, monkeypatch, httpx_mock, caplog):
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
            assert job_ids == {
                CALENDAR_SYNC_JOB_ID,
                DECISION_AND_ENTRY_CAPTURE_JOB_ID,
                EXIT_CAPTURE_JOB_ID,
                IBKR_GATEWAY_HEALTHCHECK_JOB_ID,
            }
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
                job_id=EXIT_CAPTURE_JOB_ID,
                jobstore="default",
                scheduled_run_time=None,
                exception=RuntimeError("boom"),
            )
            scheduler._dispatch_event(event)  # noqa: SLF001

            status = get_scheduler_status(scheduler)
            exit_job = next(j for j in status.jobs if j.job_id == EXIT_CAPTURE_JOB_ID)
            assert exit_job.last_run_status == "error"
