"""Phase 4.2/4.4/4.5 -- tests for services/scheduler.py: all three jobs
are registered correctly, and api/main.py's real lifespan starts/stops
them gracefully."""

from fastapi.testclient import TestClient

from services.scheduler import (
    CALENDAR_SYNC_JOB_ID,
    DECISION_AND_ENTRY_CAPTURE_JOB_ID,
    EXIT_CAPTURE_JOB_ID,
    build_scheduler,
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
        assert len(app1.state.scheduler.get_jobs()) == 3

    app2 = main_module.create_app()
    with TestClient(app2):
        jobs = app2.state.scheduler.get_jobs()
        assert len(jobs) == 3
        assert app2.state.scheduler.get_job(CALENDAR_SYNC_JOB_ID) is not None
        assert app2.state.scheduler.get_job(DECISION_AND_ENTRY_CAPTURE_JOB_ID) is not None
        assert app2.state.scheduler.get_job(EXIT_CAPTURE_JOB_ID) is not None


def test_scheduler_starts_and_shuts_down_gracefully_with_the_app():
    import api.main as main_module

    app = main_module.create_app()
    with TestClient(app):
        assert app.state.scheduler is not None
        assert app.state.scheduler.running is True
        assert app.state.scheduler.get_job(CALENDAR_SYNC_JOB_ID) is not None
        assert app.state.scheduler.get_job(DECISION_AND_ENTRY_CAPTURE_JOB_ID) is not None
        assert app.state.scheduler.get_job(EXIT_CAPTURE_JOB_ID) is not None

    assert app.state.scheduler.running is False
