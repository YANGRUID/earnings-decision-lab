"""Operations Scheduler Job Monitor lists every job the live scheduler has
registered (activation phase, Sections 51/54).

Before this, the monitor enumerated a fixed tuple of official ids, so an
optional cohort's jobs -- registered only while that cohort is enabled --
were invisible in Operations even though the job store held them. The
official set must still always be listed, even when absent (a missing
official job is a visible enabled=False row, never a silent omission).
"""

from datetime import UTC, datetime

from services.operations import ALL_JOB_IDS, get_scheduler_jobs
from services.scheduler import SchedulerJobStatus, SchedulerStatus

NEXT = datetime(2026, 9, 2, 19, 30, tzinfo=UTC)
EXTRA = ["research_preparation_startup_catchup", "some_future_job"]


def _status(ids):
    return SchedulerStatus(
        running=True,
        jobs=[
            SchedulerJobStatus(job_id=i, next_run_time=NEXT, last_run_at=None, last_run_status=None)
            for i in ids
        ],
    )


class TestRegisteredJobsAreListed:
    def test_official_jobs_are_always_listed_even_when_absent(self, db_session):
        views = get_scheduler_jobs(db_session, _status([]))
        assert [v.job_id for v in views] == list(ALL_JOB_IDS)
        assert all(v.enabled is False for v in views)

    def test_additional_registered_jobs_are_appended_in_registration_order(self, db_session):
        views = get_scheduler_jobs(db_session, _status(list(ALL_JOB_IDS) + EXTRA))
        assert [v.job_id for v in views] == list(ALL_JOB_IDS) + EXTRA
        for extra in views[-2:]:
            assert extra.enabled is True
            assert extra.next_run_time == NEXT

    def test_unregistered_optional_jobs_do_not_appear(self, db_session):
        ids = [v.job_id for v in get_scheduler_jobs(db_session, _status(list(ALL_JOB_IDS)))]
        assert ids == list(ALL_JOB_IDS)
        assert "some_future_job" not in ids
