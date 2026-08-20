"""Phase 4.2 -- tests for services/scheduler.py: the job is registered
correctly, and api/main.py's real lifespan starts/stops it gracefully."""

from fastapi.testclient import TestClient

from services.scheduler import CALENDAR_SYNC_JOB_ID, build_scheduler


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


def test_job_persists_across_app_restart_without_duplicating():
    """SQLAlchemyJobStore persists the job registration to the database;
    a second, independent app instance (simulating a process restart)
    picks up the persisted job, and replace_existing=True means
    re-registering it doesn't create a second, competing one -- the real
    mechanism behind "job survives restart"."""
    import api.main as main_module

    app1 = main_module.create_app()
    with TestClient(app1):
        assert len(app1.state.scheduler.get_jobs()) == 1

    app2 = main_module.create_app()
    with TestClient(app2):
        jobs = app2.state.scheduler.get_jobs()
        assert len(jobs) == 1
        assert app2.state.scheduler.get_job(CALENDAR_SYNC_JOB_ID) is not None


def test_scheduler_starts_and_shuts_down_gracefully_with_the_app():
    import api.main as main_module

    app = main_module.create_app()
    with TestClient(app):
        assert app.state.scheduler is not None
        assert app.state.scheduler.running is True
        assert app.state.scheduler.get_job(CALENDAR_SYNC_JOB_ID) is not None

    assert app.state.scheduler.running is False
