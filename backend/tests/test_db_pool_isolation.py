"""Scheduler-owned work uses the dedicated scheduler pool; API requests
use the API pool (V4 consolidation, Section 9).

db/session.py keeps two engines against the same database on purpose:
Operations Monitor polling alone was observed exhausting a shared pool
and starving scheduled jobs of a connection. These tests pin which side
each caller is on, by inspecting the import bindings at module level --
what the code would actually use, not what a docstring says.
"""

import db.session as session_module


class TestSchedulerJobsUseTheSchedulerPool:
    def test_v3_official_scheduler_uses_scheduler_pool(self):
        import services.scheduler as v3

        assert v3.SessionLocal is session_module.SchedulerSessionLocal
        assert v3.engine is session_module.scheduler_engine

    def test_v4_shadow_scheduler_uses_scheduler_pool(self):
        """Regression: this module imported the API-facing SessionLocal
        until 2026-09-02, so shadow jobs would have competed with user
        requests for connections."""
        import services.v4_shadow_scheduler as v4

        assert v4.SessionLocal is session_module.SchedulerSessionLocal
        assert v4.SessionLocal is not session_module.SessionLocal or (
            # Only equal if the test harness rebound both to one engine --
            # in which case identity with the scheduler factory still holds.
            session_module.SessionLocal is session_module.SchedulerSessionLocal
        )


class TestApiUsesTheApiPool:
    def test_fastapi_dependency_uses_api_pool(self):
        """Do not accidentally reverse them: HTTP requests must stay on the
        API pool so a stalled scheduler job can never block a user."""
        import api.deps as deps

        assert deps.SessionLocal is session_module.SessionLocal


class TestTheTwoPoolsAreDistinctInProduction:
    def test_engines_are_separate_objects_outside_the_test_harness(self):
        """conftest rebinds both factories to the test engine (so tests
        cannot reach production). This guards the PRODUCTION source: the
        two factories must be built from two distinct engines there."""
        import inspect

        source = inspect.getsource(session_module)
        assert "scheduler_engine = create_engine(" in source
        assert "SchedulerSessionLocal = sessionmaker(" in source
        assert "bind=scheduler_engine" in source
