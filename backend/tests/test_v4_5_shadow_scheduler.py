"""V4.5 -- shadow scheduler registration and V3 isolation
(Sections 33, 34, 38, 99).

The property that matters most: with the activation flag OFF, the shadow
jobs do not exist in the job store at all. Not registered-then-skipped --
absent. That way there is nothing to fire accidentally, and Operations
cannot report an active-but-failing job for a cohort that is switched off.
"""

from unittest.mock import patch

import pytest

from services.scheduler import (
    CALENDAR_SYNC_JOB_ID,
    EARNINGS_RESEARCH_PREPARATION_JOB_ID,
    V4_FORWARD_WINDOW_JOB_ID,
    V4_SHADOW_DECISION_JOB_ID,
    V4_SHADOW_SETTLEMENT_JOB_ID,
    build_scheduler,
)


def _settings_with(enabled: bool):
    from core.config import Settings, get_settings

    base = get_settings().model_dump()
    base["v4_shadow_enabled"] = enabled
    return Settings(**base)


def _job_ids(enabled: bool) -> set[str]:
    # build_scheduler() returns a scheduler that has NOT been started
    # (api/main.py starts it separately), so there is nothing to shut
    # down here -- calling shutdown() on it raises SchedulerNotRunningError.
    with patch("services.scheduler.get_settings", return_value=_settings_with(enabled)):
        return {job.id for job in build_scheduler().get_jobs()}


class TestShadowSchedulerRegistration:
    def test_flag_off_registers_no_shadow_jobs(self):
        """Section 34/99 -- the production default. Absent, not skipped."""
        ids = _job_ids(enabled=False)
        assert V4_FORWARD_WINDOW_JOB_ID not in ids
        assert V4_SHADOW_DECISION_JOB_ID not in ids
        assert V4_SHADOW_SETTLEMENT_JOB_ID not in ids

    def test_flag_off_leaves_platform_jobs_untouched(self):
        """V4 being disabled must not disturb the official schedule."""
        ids = _job_ids(enabled=False)
        assert CALENDAR_SYNC_JOB_ID in ids
        assert EARNINGS_RESEARCH_PREPARATION_JOB_ID in ids
        assert "decision_and_entry_capture" not in ids  # V3 retired 2026-09-02
        assert "exit_capture" not in ids

    def test_flag_on_registers_the_single_forward_window_job(self):
        """Settlement-priority hardening (v4.0.0): ONE 15:30 job. The two
        historical ids are its recorded phases, never separate registrations
        (two registrations on one cron had no defined order)."""
        ids = _job_ids(enabled=True)
        assert V4_FORWARD_WINDOW_JOB_ID in ids
        assert V4_SHADOW_DECISION_JOB_ID not in ids
        assert V4_SHADOW_SETTLEMENT_JOB_ID not in ids

    def test_flag_on_still_keeps_platform_jobs(self):
        ids = _job_ids(enabled=True)
        assert CALENDAR_SYNC_JOB_ID in ids
        assert EARNINGS_RESEARCH_PREPARATION_JOB_ID in ids
        assert "decision_and_entry_capture" not in ids  # V3 retired 2026-09-02
        assert "exit_capture" not in ids

    def test_shadow_job_ids_are_distinct_from_official_ids(self):
        """Section 52 -- must never overload the official V3 job's own
        success/failure counters."""
        official = {CALENDAR_SYNC_JOB_ID, EARNINGS_RESEARCH_PREPARATION_JOB_ID}
        shadow = {V4_SHADOW_DECISION_JOB_ID, V4_SHADOW_SETTLEMENT_JOB_ID}
        assert not (official & shadow)

    def test_the_forward_window_fires_at_1530_eastern_and_owns_both_phases(self):
        """V4-only reset (2026-09-02) + settlement priority (v4.0.0): decision/
        entry and the T+1 settlement both belong to the 15:30 ET forward window,
        which is ONE registration with a defined order (settle, then decide)."""
        with patch("services.scheduler.get_settings", return_value=_settings_with(True)):
            jobs = {job.id: job for job in build_scheduler().get_jobs()}

        window = {f.name: str(f) for f in jobs[V4_FORWARD_WINDOW_JOB_ID].trigger.fields}
        assert (window["hour"], window["minute"]) == ("15", "30")
        assert str(jobs[V4_FORWARD_WINDOW_JOB_ID].trigger.timezone) == "America/New_York"
        assert jobs[V4_FORWARD_WINDOW_JOB_ID].max_instances == 1
        assert V4_SHADOW_DECISION_JOB_ID not in jobs and V4_SHADOW_SETTLEMENT_JOB_ID not in jobs

    def test_activation_is_never_a_code_default(self):
        """Activated in production on 2026-09-02 by an explicit environment
        decision after the live gate. The code default stays False: a
        fresh deployment never registers the shadow jobs implicitly."""
        from core.config import Settings

        assert Settings(_env_file=None).v4_shadow_enabled is False


class TestShadowJobSafety:
    @pytest.mark.parametrize("job_name", ["run_v4_forward_window_job"])
    def test_shadow_job_refuses_to_act_when_flag_is_off(self, job_name, db_session):
        """Defence in depth (Section 34): even if a stale job somehow
        remained registered, it must do nothing while disabled.

        NOTE the patch target. The job BODIES live in
        services.v4_shadow_scheduler and read get_settings from that
        module's own namespace -- patching services.scheduler.get_settings
        would not reach them, and this test would then pass vacuously
        (the real flag is False anyway) while asserting nothing."""
        import services.v4_shadow_scheduler as jobs_module

        job = getattr(jobs_module, job_name)
        with patch.object(jobs_module, "get_settings", return_value=_settings_with(False)):
            job()  # must not raise, must not create shadow evidence

        from models.v4_shadow import V4ShadowDecision

        assert db_session.query(V4ShadowDecision).count() == 0

    def test_shadow_registration_failure_does_not_lose_official_v3_jobs(self):
        """The most important guarantee here, found by this project's own
        V4-isolation test: api/main.py wraps build_scheduler() in a
        try/except that disables the ENTIRE scheduler on failure. If
        registering an EXPERIMENTAL V4 job could raise out of
        build_scheduler, every OFFICIAL V3 job would vanish with it."""
        import services.scheduler as scheduler_module

        with (
            patch.object(scheduler_module, "get_settings", return_value=_settings_with(True)),
            patch.object(
                scheduler_module,
                "run_v4_forward_window_job",
                side_effect=RuntimeError("unregisterable"),
            ),
        ):
            # Force add_job to blow up for the shadow job only.
            original_add_job = scheduler_module.AsyncIOScheduler.add_job

            def _explode_on_shadow(self, func, *args, **kwargs):
                if kwargs.get("id") == V4_FORWARD_WINDOW_JOB_ID:
                    raise RuntimeError("simulated shadow registration failure")
                return original_add_job(self, func, *args, **kwargs)

            with patch.object(scheduler_module.AsyncIOScheduler, "add_job", _explode_on_shadow):
                scheduler = scheduler_module.build_scheduler()

        ids = {job.id for job in scheduler.get_jobs()}
        # V3 survives the V4 failure -- which is the whole point.
        assert CALENDAR_SYNC_JOB_ID in ids
        assert EARNINGS_RESEARCH_PREPARATION_JOB_ID in ids
        assert "decision_and_entry_capture" not in ids  # V3 retired 2026-09-02
        assert "exit_capture" not in ids
        assert V4_FORWARD_WINDOW_JOB_ID not in ids

    @pytest.mark.parametrize("job_name", ["run_v4_forward_window_job"])
    def test_shadow_job_never_raises_into_the_scheduler(self, job_name):
        """Section 38 -- a V4 failure must never propagate into the
        scheduler and take the official path with it.

        Patches the module that actually owns the job body, so the
        side_effect genuinely fires (see the note above)."""
        import services.v4_shadow_scheduler as jobs_module

        job = getattr(jobs_module, job_name)
        with patch.object(jobs_module, "get_settings", side_effect=RuntimeError("boom")):
            job()  # swallowed and recorded, never raised
