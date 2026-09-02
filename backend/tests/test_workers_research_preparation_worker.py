"""Pre-live hardening (2026-08-25) -- tests for workers/research_
preparation_worker.py's own failure-isolation boundary (_process_claimed_
job). run_forever()/_run_one_cycle() themselves are deliberately not
exercised here: they load a real FastEmbedProvider (a real, heavy local
model) and drive signal handling / the real poll loop, which is real
process-level behavior this project's own established pattern (see
services/scheduler.py's own tests) doesn't unit-test directly either --
their own correctness rests on claim_next_preparation_job/recover_stale_
running_jobs (tests/test_services_research_preparation_queue.py) and
prepare_company_research (tests/test_services_research_orchestration.py)
each already being independently covered, plus the real, live restart
proof performed separately against the actual deployed containers.

What's genuinely new and worth its own coverage here: _process_claimed_
job is the ONE place a company's failure outside prepare_company_
research's own handled pipeline (e.g. a symbol that's become unsupported,
or a genuinely unexpected error) gets turned into an honest terminal
FAILED row instead of being left RUNNING forever or crashing the worker
loop -- exactly Section 20's "one company's failure isolated from the
next" and "no zombie RUNNING jobs" requirements.
"""

from datetime import UTC, datetime

from models.research_preparation_job import JobStatus, ResearchPreparationJob
from services.research_orchestration import UnsupportedSymbolError
from workers.research_preparation_worker import _process_claimed_job

NOW = datetime(2033, 6, 1, tzinfo=UTC)


def _claimed_job(db_session, *, ticker="TESTWORKER"):
    job = ResearchPreparationJob(
        ticker=ticker,
        status=JobStatus.RUNNING,
        steps=[],
        started_at=NOW,
        heartbeat_at=NOW,
        worker_id="research-worker-test",
        attempt_count=1,
    )
    db_session.add(job)
    db_session.commit()
    return job


class TestProcessClaimedJob:
    def test_unsupported_symbol_error_marks_the_row_failed_with_the_real_reason(
        self, monkeypatch, db_session
    ):
        job = _claimed_job(db_session)

        def _raise(db, ticker, providers, existing_job=None):
            raise UnsupportedSymbolError("delisted, no longer tradable")

        monkeypatch.setattr("workers.research_preparation_worker.prepare_company_research", _raise)

        _process_claimed_job(db_session, job, research_providers=None, worker_id="worker-a")

        row = db_session.get(ResearchPreparationJob, job.id)
        assert row.status == JobStatus.FAILED
        assert "delisted, no longer tradable" in row.error
        assert row.completed_at is not None

    def test_a_genuinely_unexpected_exception_marks_the_row_failed_not_left_running(
        self, monkeypatch, db_session
    ):
        """The real boundary this exists for: a failure OUTSIDE prepare_
        company_research's own handled pipeline (e.g. building providers
        itself blows up) must still leave an honest terminal state, never
        a permanently-RUNNING zombie row with no further heartbeat."""
        job = _claimed_job(db_session)

        def _raise(db, ticker, providers, existing_job=None):
            raise RuntimeError("simulated unexpected provider construction failure")

        monkeypatch.setattr("workers.research_preparation_worker.prepare_company_research", _raise)

        _process_claimed_job(db_session, job, research_providers=None, worker_id="worker-a")

        row = db_session.get(ResearchPreparationJob, job.id)
        assert row.status == JobStatus.FAILED
        assert "simulated unexpected provider construction failure" in row.error
        assert row.completed_at is not None

    def test_one_companys_failure_never_raises_out_of_process_claimed_job(
        self, monkeypatch, db_session
    ):
        """The worker's own poll loop calls this with no surrounding
        try/except of its own around a single company (see run_forever's
        docstring) -- so this function itself must never propagate a
        company-level failure, or one bad company would crash the whole
        worker process."""
        job = _claimed_job(db_session)

        def _raise(db, ticker, providers, existing_job=None):
            raise RuntimeError("simulated failure")

        monkeypatch.setattr("workers.research_preparation_worker.prepare_company_research", _raise)

        _process_claimed_job(db_session, job, research_providers=None, worker_id="worker-a")
        # no exception propagated -- reaching this line is the assertion

    def test_a_normal_successful_run_is_left_exactly_as_prepare_company_research_set_it(
        self, monkeypatch, db_session
    ):
        """prepare_company_research itself owns the row's final state on
        a real (non-exception) run -- _process_claimed_job must not
        second-guess or overwrite it."""
        job = _claimed_job(db_session)

        def _fake_success(db, ticker, providers, existing_job=None):
            existing_job.status = JobStatus.COMPLETED
            existing_job.completed_at = NOW
            db.add(existing_job)
            db.commit()
            return existing_job

        monkeypatch.setattr(
            "workers.research_preparation_worker.prepare_company_research", _fake_success
        )

        _process_claimed_job(db_session, job, research_providers=None, worker_id="worker-a")

        row = db_session.get(ResearchPreparationJob, job.id)
        assert row.status == JobStatus.COMPLETED
        assert row.error is None
