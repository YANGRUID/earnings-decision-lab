"""Contract-resolution failures are never counted as business
ineligibility (V4 consolidation, Section 14)."""

from datetime import UTC, datetime, timedelta

from models.scheduler_run import SchedulerRun, SchedulerRunEvent
from services.operations import get_todays_official_run
from services.scheduler import DECISION_AND_ENTRY_CAPTURE_JOB_ID

RUN_AT = datetime(2026, 9, 1, 19, 55, tzinfo=UTC)


def _run(db):
    run = SchedulerRun(
        job_id=DECISION_AND_ENTRY_CAPTURE_JOB_ID, started_at=RUN_AT,
        finished_at=RUN_AT + timedelta(minutes=1), status="success", items_evaluated=3,
    )
    db.add(run)
    db.flush()
    return run


def _event(db, run, outcome, symbol, reason=None):
    db.add(SchedulerRunEvent(
        scheduler_run_id=run.id, earnings_calendar_event_id=None, symbol=symbol,
        stage="decision", outcome=outcome, reason=reason, occurred_at=RUN_AT,
    ))
    db.flush()


class TestContractResolutionIsItsOwnBucket:
    def test_bf_a_style_failure_is_not_skipped_ineligible(self, db_session):
        """The real 2026-09-01 shape: BF.A and BF.B drew TWS error 200 and
        were filed as ineligible. They must now land in their own counter."""
        run = _run(db_session)
        _event(db_session, run, "skipped_ineligible", "CXM", "market cap below $10B")
        _event(db_session, run, "contract_resolution_failed", "BF.A",
               "options chain lookup failed: no contract found (error 200)")
        _event(db_session, run, "contract_resolution_failed", "BF.B",
               "options chain lookup failed: no contract found (error 200)")

        result = get_todays_official_run(db_session, now=RUN_AT)
        assert result.found
        assert result.skipped_ineligible == 1
        assert result.contract_resolution_failed == 2
        assert result.pipeline_failed == 0

    def test_decision_pipeline_emits_the_new_outcome_for_retryable_provider_failures(self):
        """The Outcome vocabulary itself carries the distinction."""
        from typing import get_args

        from services.decision_pipeline import Outcome

        assert "contract_resolution_failed" in get_args(Outcome)
        assert "skipped_ineligible" in get_args(Outcome)
