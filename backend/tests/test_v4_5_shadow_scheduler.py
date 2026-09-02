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
    DECISION_AND_ENTRY_CAPTURE_JOB_ID,
    EXIT_CAPTURE_JOB_ID,
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
        assert V4_SHADOW_DECISION_JOB_ID not in ids
        assert V4_SHADOW_SETTLEMENT_JOB_ID not in ids

    def test_flag_off_leaves_official_v3_jobs_untouched(self):
        """V4 being disabled must not disturb the official schedule."""
        ids = _job_ids(enabled=False)
        assert DECISION_AND_ENTRY_CAPTURE_JOB_ID in ids
        assert EXIT_CAPTURE_JOB_ID in ids

    def test_flag_on_registers_both_shadow_jobs(self):
        """Section 99 -- proven in an ISOLATED test config only; the
        production flag is not changed by this test."""
        ids = _job_ids(enabled=True)
        assert V4_SHADOW_DECISION_JOB_ID in ids
        assert V4_SHADOW_SETTLEMENT_JOB_ID in ids

    def test_flag_on_still_keeps_official_jobs(self):
        ids = _job_ids(enabled=True)
        assert DECISION_AND_ENTRY_CAPTURE_JOB_ID in ids
        assert EXIT_CAPTURE_JOB_ID in ids

    def test_shadow_job_ids_are_distinct_from_official_ids(self):
        """Section 52 -- must never overload the official V3 job's own
        success/failure counters."""
        official = {DECISION_AND_ENTRY_CAPTURE_JOB_ID, EXIT_CAPTURE_JOB_ID}
        shadow = {V4_SHADOW_DECISION_JOB_ID, V4_SHADOW_SETTLEMENT_JOB_ID}
        assert not (official & shadow)

    def test_v4_decision_runs_at_its_own_1530_policy_not_v3s_1555(self):
        """V4 product consolidation (2026-09-02) -- the V4 DECISION
        observation moves to 15:30 ET while V3 stays at 15:55 ET.

        This test previously asserted the opposite (identical crons). That
        assertion encoded the old methodology and is deliberately replaced,
        not deleted: the requirement it protected -- that V4 never gets a
        post-event timing advantage -- still holds, and is now stronger,
        because 15:30 is EARLIER than V3's 15:55. V4 sees less of the
        session, never more.
        """
        with patch("services.scheduler.get_settings", return_value=_settings_with(True)):
            jobs = {job.id: job for job in build_scheduler().get_jobs()}

        # Compare by field NAME rather than positional index -- APScheduler's
        # field order is an implementation detail, and an index that silently
        # shifted would make this assertion pass while comparing the wrong
        # thing entirely.
        def fields(job_id):
            return {f.name: str(f) for f in jobs[job_id].trigger.fields}

        official = fields(DECISION_AND_ENTRY_CAPTURE_JOB_ID)
        shadow_decision = fields(V4_SHADOW_DECISION_JOB_ID)

        assert official["hour"] == "15" and official["minute"] == "55"
        assert shadow_decision["hour"] == "15" and shadow_decision["minute"] == "30"
        # Same timezone -- only the minute differs, never the clock itself.
        assert str(jobs[DECISION_AND_ENTRY_CAPTURE_JOB_ID].trigger.timezone) == str(
            jobs[V4_SHADOW_DECISION_JOB_ID].trigger.timezone
        )
        # V4 observes EARLIER than V3, never later: no post-event advantage.
        assert int(shadow_decision["minute"]) < int(official["minute"])

    def test_v4_settlement_did_not_move_with_the_decision_time(self):
        """Entry timing and settlement timing are separate policies
        (Sections 26/56). The four jobs used to share one constant pair, so
        the real hazard here is an edit that moves settlement along with the
        decision. Pin all four explicitly."""
        with patch("services.scheduler.get_settings", return_value=_settings_with(True)):
            jobs = {job.id: job for job in build_scheduler().get_jobs()}

        def hhmm(job_id):
            f = {x.name: str(x) for x in jobs[job_id].trigger.fields}
            return f["hour"], f["minute"]

        assert hhmm(DECISION_AND_ENTRY_CAPTURE_JOB_ID) == ("15", "55")  # V3 entry
        assert hhmm(EXIT_CAPTURE_JOB_ID) == ("15", "55")                # V3 exit
        assert hhmm(V4_SHADOW_DECISION_JOB_ID) == ("15", "30")          # V4 entry: moved
        assert hhmm(V4_SHADOW_SETTLEMENT_JOB_ID) == ("15", "55")        # V4 exit: unmoved

    def test_production_flag_is_still_off(self):
        """Guards against this task accidentally shipping activated."""
        from core.config import get_settings

        assert get_settings().v4_shadow_enabled is False


class TestShadowJobSafety:
    @pytest.mark.parametrize(
        "job_name",
        ["run_v4_shadow_decision_job", "run_v4_shadow_settlement_job"],
    )
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

        with patch.object(scheduler_module, "get_settings", return_value=_settings_with(True)), \
             patch.object(
                 scheduler_module,
                 "run_v4_shadow_decision_job",
                 side_effect=RuntimeError("unregisterable"),
             ):
            # Force add_job to blow up for the shadow job only.
            original_add_job = scheduler_module.AsyncIOScheduler.add_job

            def _explode_on_shadow(self, func, *args, **kwargs):
                if kwargs.get("id") == V4_SHADOW_DECISION_JOB_ID:
                    raise RuntimeError("simulated shadow registration failure")
                return original_add_job(self, func, *args, **kwargs)

            with patch.object(
                scheduler_module.AsyncIOScheduler, "add_job", _explode_on_shadow
            ):
                scheduler = scheduler_module.build_scheduler()

        ids = {job.id for job in scheduler.get_jobs()}
        # V3 survives the V4 failure -- which is the whole point.
        assert DECISION_AND_ENTRY_CAPTURE_JOB_ID in ids
        assert EXIT_CAPTURE_JOB_ID in ids
        assert V4_SHADOW_DECISION_JOB_ID not in ids

    @pytest.mark.parametrize(
        "job_name",
        ["run_v4_shadow_decision_job", "run_v4_shadow_settlement_job"],
    )
    def test_shadow_job_never_raises_into_the_scheduler(self, job_name):
        """Section 38 -- a V4 failure must never propagate into the
        scheduler and take the official path with it.

        Patches the module that actually owns the job body, so the
        side_effect genuinely fires (see the note above)."""
        import services.v4_shadow_scheduler as jobs_module

        job = getattr(jobs_module, job_name)
        with patch.object(jobs_module, "get_settings", side_effect=RuntimeError("boom")):
            job()  # swallowed and recorded, never raised
