"""Operations Monitor -- tests for services/operations.py.

derive_lifecycle_state and compute_execution_summary are pure functions
(no DB/network access), so most of the state-machine coverage lives
there, fast and exhaustive. The DB-touching aggregations
(get_todays_pipeline, get_scheduler_jobs, get_preflight_readiness,
get_recent_failures) are tested against db_session with far-future
dates/timestamps, exactly like tests/test_services_earnings_calendar_
sync.py's own established convention -- this suite runs against a real,
shared dev Postgres instance that already has real committed rows from
real syncs, so a test window that overlapped them would see them too.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from analytics.earnings_timing import AnnouncementTime, compute_entry_exit_schedule
from models.benchmark_portfolio import BenchmarkPortfolio
from models.decision_snapshot import DecisionSnapshot
from models.earnings_calendar_event import EarningsCalendarEvent
from models.entry_capture_attempt import EntryCaptureAttempt
from models.enums import (
    CaptureStatus,
    DecisionDirection,
    DecisionSnapshotStatus,
    EarningsCalendarEventStatus,
    EarningsSource,
    EarningsTiming,
    ProviderHealthStatus,
    RiskProfile,
)
from models.provider_health_event import ProviderHealthEvent
from models.scheduler_run import SchedulerRun, SchedulerRunEvent
from models.settlement_capture_attempt import SettlementCaptureAttempt
from services.decision_pipeline import LATE_CUTOFF_GRACE
from services.operations import (
    STATE_CALENDAR_DISCOVERED,
    STATE_ENTRY_FAILED,
    STATE_FILTERED_OUT,
    STATE_NO_ACTION,
    STATE_NOT_ELIGIBLE,
    STATE_SETTLED,
    STATE_SETTLEMENT_FAILED,
    STATE_SKIPPED,
    STATE_WAITING_FOR_DECISION,
    STATE_WAITING_FOR_ENTRY,
    STATE_WAITING_FOR_SETTLEMENT,
    compute_execution_summary,
    derive_lifecycle_state,
    get_preflight_readiness,
    get_recent_failures,
    get_scheduler_jobs,
    get_todays_official_run,
    get_todays_pipeline,
)
from services.scheduler import (
    CALENDAR_SYNC_JOB_ID,
    DECISION_AND_ENTRY_CAPTURE_JOB_ID,
    EXIT_CAPTURE_JOB_ID,
)
from services.scheduler_run_tracking import (
    OUTCOME_DECISION_NO_ACTION,
    OUTCOME_ENTRY_CAPTURED,
    OUTCOME_ENTRY_FAILED,
    OUTCOME_SETTLEMENT_CAPTURED,
    OUTCOME_SETTLEMENT_FAILED,
)

# A real due-time-shaped instant, always used as both "now" and the real
# runs' own started_at below -- both stay on the same New York calendar
# date, which is the only thing get_todays_official_run cares about.
TODAY_RUN_AT = datetime(2031, 3, 12, 19, 55, tzinfo=UTC)

# Far-future so this never collides with real, already-synced calendar
# data (see module docstring).
FAR_FUTURE_EARNINGS_DATE = date(2031, 3, 12)  # a real Wednesday


def _schedule(earnings_date=FAR_FUTURE_EARNINGS_DATE, session=AnnouncementTime.AFTER_MARKET):
    return compute_entry_exit_schedule(earnings_date, session)


class TestDeriveLifecycleState:
    def test_calendar_discovered_when_nothing_has_happened_and_not_due(self):
        schedule = _schedule()
        result = derive_lifecycle_state(
            schedule=schedule,
            now=schedule.entry_timestamp - timedelta(days=1),
            latest_decision_outcome=None,
            latest_decision_reason=None,
            decision_snapshot=None,
            latest_entry_attempt=None,
            latest_settlement_attempt=None,
        )
        assert result.state == STATE_CALENDAR_DISCOVERED
        assert result.next_action_at == schedule.entry_timestamp

    def test_filtered_out_from_a_real_preparation_scheduler_run_event(self):
        from services.operations import STATE_FILTERED_OUT

        schedule = _schedule()
        result = derive_lifecycle_state(
            schedule=schedule,
            now=schedule.entry_timestamp - timedelta(days=1),
            latest_decision_outcome=None,
            latest_decision_reason=None,
            decision_snapshot=None,
            latest_entry_attempt=None,
            latest_settlement_attempt=None,
            latest_preparation_outcome="filtered_out",
            latest_preparation_reason="market cap below $10,000,000,000",
        )
        assert result.state == STATE_FILTERED_OUT
        assert result.reason == "market cap below $10,000,000,000"
        assert result.next_action is None  # nothing more to do, same as NOT_ELIGIBLE

    def test_ready_for_decision_after_successful_preparation(self):
        from services.operations import STATE_READY_FOR_DECISION

        schedule = _schedule()
        result = derive_lifecycle_state(
            schedule=schedule,
            now=schedule.entry_timestamp - timedelta(days=1),
            latest_decision_outcome=None,
            latest_decision_reason=None,
            decision_snapshot=None,
            latest_entry_attempt=None,
            latest_settlement_attempt=None,
            latest_preparation_outcome="prepared",
            latest_preparation_reason=None,
        )
        assert result.state == STATE_READY_FOR_DECISION
        assert result.next_action_at == schedule.entry_timestamp

    def test_ready_for_decision_when_reusing_an_already_prepared_company(self):
        from services.operations import STATE_READY_FOR_DECISION

        schedule = _schedule()
        result = derive_lifecycle_state(
            schedule=schedule,
            now=schedule.entry_timestamp - timedelta(days=1),
            latest_decision_outcome=None,
            latest_decision_reason=None,
            decision_snapshot=None,
            latest_entry_attempt=None,
            latest_settlement_attempt=None,
            latest_preparation_outcome="already_prepared",
            latest_preparation_reason=None,
        )
        assert result.state == STATE_READY_FOR_DECISION

    def test_preparation_failed_from_a_real_preparation_scheduler_run_event(self):
        from services.operations import STATE_PREPARATION_FAILED

        schedule = _schedule()
        result = derive_lifecycle_state(
            schedule=schedule,
            now=schedule.entry_timestamp - timedelta(days=1),
            latest_decision_outcome=None,
            latest_decision_reason=None,
            decision_snapshot=None,
            latest_entry_attempt=None,
            latest_settlement_attempt=None,
            latest_preparation_outcome="preparation_failed",
            latest_preparation_reason="SEC EDGAR outage",
        )
        assert result.state == STATE_PREPARATION_FAILED
        assert result.reason == "SEC EDGAR outage"
        assert result.next_action == "Retry research preparation"

    def test_due_event_reaches_waiting_for_decision_even_if_preparation_never_ran(self):
        """The official decision path never depends on preparation having
        succeeded (it does its own independent eligibility/company
        check) -- once due, WAITING_FOR_DECISION always wins over any
        preparation-stage state, preparation outcome notwithstanding."""
        result = derive_lifecycle_state(
            schedule=_schedule(),
            now=_schedule().entry_timestamp + timedelta(minutes=1),
            latest_decision_outcome=None,
            latest_decision_reason=None,
            decision_snapshot=None,
            latest_entry_attempt=None,
            latest_settlement_attempt=None,
            latest_preparation_outcome="preparation_failed",
            latest_preparation_reason="SEC EDGAR outage",
        )
        assert result.state == STATE_WAITING_FOR_DECISION

    def test_waiting_for_decision_when_due_but_nothing_recorded_yet(self):
        schedule = _schedule()
        result = derive_lifecycle_state(
            schedule=schedule,
            now=schedule.entry_timestamp + timedelta(minutes=1),
            latest_decision_outcome=None,
            latest_decision_reason=None,
            decision_snapshot=None,
            latest_entry_attempt=None,
            latest_settlement_attempt=None,
        )
        assert result.state == STATE_WAITING_FOR_DECISION

    def test_not_eligible_from_a_real_scheduler_run_event(self):
        schedule = _schedule()
        result = derive_lifecycle_state(
            schedule=schedule,
            now=schedule.entry_timestamp + timedelta(minutes=1),
            latest_decision_outcome="skipped_ineligible",
            latest_decision_reason="market cap below $10,000,000,000",
            decision_snapshot=None,
            latest_entry_attempt=None,
            latest_settlement_attempt=None,
        )
        assert result.state == STATE_NOT_ELIGIBLE
        assert result.reason == "market cap below $10,000,000,000"
        assert result.next_action is None  # nothing more to do -- genuinely ineligible

    def test_skipped_when_decision_pipeline_itself_failed(self):
        schedule = _schedule()
        result = derive_lifecycle_state(
            schedule=schedule,
            now=schedule.entry_timestamp + timedelta(minutes=1),
            latest_decision_outcome="failed",
            latest_decision_reason="LLM provider timeout",
            decision_snapshot=None,
            latest_entry_attempt=None,
            latest_settlement_attempt=None,
        )
        assert result.state == STATE_SKIPPED
        assert result.reason == "LLM provider timeout"

    def test_decision_generated_but_entry_window_not_yet_due(self):
        schedule = _schedule()
        decision = DecisionSnapshot(
            id=1,
            earnings_calendar_event_id=1,
            benchmark_portfolio_id=1,
            ticker="TEST",
            company_name="Test Co",
            strategy_direction=DecisionDirection.BULLISH,
            generated_at=schedule.entry_timestamp,
            status=DecisionSnapshotStatus.PENDING_ENTRY,
            engine_version="v3",
            prompt_version="v1",
            expiration_source="v3_auto_resolver",
            # A real, actionable strategy -- distinguishes this fixture
            # from the genuine no-action case (empty/None legs), which
            # has its own dedicated tests below.
            legs=[
                {
                    "option_type": "call",
                    "action": "buy",
                    "strike": "100.00",
                    "premium": "5.00",
                    "quantity": 1,
                }
            ],
        )
        result = derive_lifecycle_state(
            schedule=schedule,
            now=schedule.entry_timestamp,  # exactly at generation, entry not attempted yet
            latest_decision_outcome="created",
            latest_decision_reason=None,
            decision_snapshot=decision,
            latest_entry_attempt=None,
            latest_settlement_attempt=None,
        )
        assert result.state in (STATE_WAITING_FOR_ENTRY, "DECISION_GENERATED")

    def test_waiting_for_entry_when_decision_exists_and_window_passed(self):
        schedule = _schedule()
        decision = DecisionSnapshot(
            id=1,
            earnings_calendar_event_id=1,
            benchmark_portfolio_id=1,
            ticker="TEST",
            company_name="Test Co",
            strategy_direction=DecisionDirection.BULLISH,
            generated_at=schedule.entry_timestamp,
            status=DecisionSnapshotStatus.PENDING_ENTRY,
            engine_version="v3",
            prompt_version="v1",
            expiration_source="v3_auto_resolver",
            # A real, actionable strategy -- distinguishes this fixture
            # from the genuine no-action case (empty/None legs), which
            # has its own dedicated tests below.
            legs=[
                {
                    "option_type": "call",
                    "action": "buy",
                    "strike": "100.00",
                    "premium": "5.00",
                    "quantity": 1,
                }
            ],
        )
        result = derive_lifecycle_state(
            schedule=schedule,
            now=schedule.entry_timestamp + timedelta(hours=1),
            latest_decision_outcome="created",
            latest_decision_reason=None,
            decision_snapshot=decision,
            latest_entry_attempt=None,
            latest_settlement_attempt=None,
        )
        assert result.state == STATE_WAITING_FOR_ENTRY

    def test_entry_failed_shows_the_real_capture_error(self):
        schedule = _schedule()
        entry = EntryCaptureAttempt(
            id=1,
            decision_snapshot_id=1,
            benchmark_portfolio_id=1,
            status=CaptureStatus.FAILED,
            capture_error="options provider call failed: no usable option quotes",
        )
        result = derive_lifecycle_state(
            schedule=schedule,
            now=schedule.entry_timestamp,
            latest_decision_outcome="created",
            latest_decision_reason=None,
            decision_snapshot=None,
            latest_entry_attempt=entry,
            latest_settlement_attempt=None,
        )
        assert result.state == STATE_ENTRY_FAILED
        assert "no usable option quotes" in result.reason
        assert result.next_action == "Retry entry capture"

    def test_entry_failed_hides_retry_once_the_legal_window_has_closed(self):
        """Post-live correction (2026-08-25): real Operations showed
        "Retry entry capture" for Aug 25's failed entries hours after the
        official ±5-minute capture window closed -- server-side
        enforcement (capture_benchmark_entry's own _verify_no_lookahead)
        already refuses a stale retry, but the READ side must stop
        advertising an action that can never succeed."""
        schedule = _schedule()
        entry = EntryCaptureAttempt(
            id=1,
            decision_snapshot_id=1,
            benchmark_portfolio_id=1,
            status=CaptureStatus.FAILED,
            capture_error="no ask quote available for a long leg",
        )
        result = derive_lifecycle_state(
            schedule=schedule,
            now=schedule.entry_timestamp + LATE_CUTOFF_GRACE + timedelta(hours=3),
            latest_decision_outcome="created",
            latest_decision_reason=None,
            decision_snapshot=None,
            latest_entry_attempt=entry,
            latest_settlement_attempt=None,
        )
        assert result.state == STATE_ENTRY_FAILED
        assert result.next_action is None

    def test_no_action_when_decision_has_no_recommended_legs(self):
        """Post-live correction (2026-08-25): real Aug 25 evidence -- SJM's
        DecisionSnapshot had strategy_type=None, legs=None (the engine
        genuinely found nothing actionable, see services/decision_
        snapshot_freezing.py), yet capture_benchmark_entry still records
        a FAILED EntryCaptureAttempt for it ("no recommended strategy
        legs to enter"), which used to collapse into the same
        ENTRY_FAILED state as a real infrastructure failure."""
        schedule = _schedule()
        decision = DecisionSnapshot(
            id=1,
            earnings_calendar_event_id=1,
            benchmark_portfolio_id=1,
            ticker="TEST",
            company_name="Test Co",
            strategy_direction=DecisionDirection.NEUTRAL,
            generated_at=schedule.entry_timestamp,
            status=DecisionSnapshotStatus.PENDING_ENTRY,
            engine_version="v3",
            prompt_version="v1",
            expiration_source="v3_auto_resolver",
            legs=None,
        )
        entry = EntryCaptureAttempt(
            id=1,
            decision_snapshot_id=1,
            benchmark_portfolio_id=1,
            status=CaptureStatus.FAILED,
            capture_error="decision_snapshot has no recommended strategy legs to enter",
        )
        result = derive_lifecycle_state(
            schedule=schedule,
            now=schedule.entry_timestamp + timedelta(minutes=1),
            latest_decision_outcome="created",
            latest_decision_reason=None,
            decision_snapshot=decision,
            latest_entry_attempt=entry,
            latest_settlement_attempt=None,
        )
        assert result.state == STATE_NO_ACTION
        assert result.state != STATE_ENTRY_FAILED

    def test_no_action_also_applies_with_empty_legs_list(self):
        schedule = _schedule()
        decision = DecisionSnapshot(
            id=1,
            earnings_calendar_event_id=1,
            benchmark_portfolio_id=1,
            ticker="TEST",
            company_name="Test Co",
            strategy_direction=DecisionDirection.NEUTRAL,
            generated_at=schedule.entry_timestamp,
            status=DecisionSnapshotStatus.PENDING_ENTRY,
            engine_version="v3",
            prompt_version="v1",
            expiration_source="v3_auto_resolver",
            legs=[],
        )
        result = derive_lifecycle_state(
            schedule=schedule,
            now=schedule.entry_timestamp - timedelta(minutes=1),
            latest_decision_outcome="created",
            latest_decision_reason=None,
            decision_snapshot=decision,
            latest_entry_attempt=None,
            latest_settlement_attempt=None,
        )
        assert result.state == STATE_NO_ACTION

    def test_filtered_out_stays_terminal_even_once_the_event_is_due(self):
        """Post-live correction (2026-08-25): real CSHR/CTRN evidence --
        both were correctly recorded filtered_out (market cap below $10B)
        at preparation time, but by the time Operations was read that
        afternoon their BMO entry_timestamp had already passed, and the
        old branch order let "now >= entry_timestamp" fire first,
        silently reverting a terminal hard-filter rejection back to
        WAITING_FOR_DECISION as if it might still resolve into a real
        decision."""
        schedule = _schedule()
        result = derive_lifecycle_state(
            schedule=schedule,
            now=schedule.entry_timestamp + timedelta(hours=6),  # due date long since passed
            latest_decision_outcome=None,  # the decision job never evaluated it either
            latest_decision_reason=None,
            decision_snapshot=None,
            latest_entry_attempt=None,
            latest_settlement_attempt=None,
            latest_preparation_outcome="filtered_out",
            latest_preparation_reason="market cap below $10,000,000,000",
        )
        assert result.state == STATE_FILTERED_OUT
        assert result.reason == "market cap below $10,000,000,000"
        assert result.next_action is None

    def test_preparation_failed_still_progresses_to_waiting_for_decision_when_due(self):
        """Unlike filtered_out (a hard, terminal rejection), a soft
        preparation failure must NOT become permanently terminal -- the
        official decision job still gets its own independent chance to
        evaluate the event once due."""
        schedule = _schedule()
        result = derive_lifecycle_state(
            schedule=schedule,
            now=schedule.entry_timestamp + timedelta(hours=1),
            latest_decision_outcome=None,
            latest_decision_reason=None,
            decision_snapshot=None,
            latest_entry_attempt=None,
            latest_settlement_attempt=None,
            latest_preparation_outcome="preparation_failed",
            latest_preparation_reason="provider timeout",
        )
        assert result.state == STATE_WAITING_FOR_DECISION

    def test_waiting_for_settlement_after_a_real_captured_entry(self):
        schedule = _schedule()
        entry = EntryCaptureAttempt(
            id=1, decision_snapshot_id=1, benchmark_portfolio_id=1, status=CaptureStatus.CAPTURED
        )
        result = derive_lifecycle_state(
            schedule=schedule,
            now=schedule.entry_timestamp,
            latest_decision_outcome="created",
            latest_decision_reason=None,
            decision_snapshot=None,
            latest_entry_attempt=entry,
            latest_settlement_attempt=None,
        )
        assert result.state == STATE_WAITING_FOR_SETTLEMENT
        assert result.next_action_at == schedule.exit_timestamp

    def test_settlement_failed_shows_the_real_capture_error(self):
        schedule = _schedule()
        entry = EntryCaptureAttempt(
            id=1, decision_snapshot_id=1, benchmark_portfolio_id=1, status=CaptureStatus.CAPTURED
        )
        settlement = SettlementCaptureAttempt(
            id=1,
            decision_snapshot_id=1,
            benchmark_portfolio_id=1,
            status=CaptureStatus.FAILED,
            capture_error="no usable exit quotes",
        )
        result = derive_lifecycle_state(
            schedule=schedule,
            now=schedule.exit_timestamp,
            latest_decision_outcome="created",
            latest_decision_reason=None,
            decision_snapshot=None,
            latest_entry_attempt=entry,
            latest_settlement_attempt=settlement,
        )
        assert result.state == STATE_SETTLEMENT_FAILED
        assert "no usable exit quotes" in result.reason

    def test_settled_is_terminal_and_needs_no_further_action(self):
        schedule = _schedule()
        entry = EntryCaptureAttempt(
            id=1, decision_snapshot_id=1, benchmark_portfolio_id=1, status=CaptureStatus.CAPTURED
        )
        settlement = SettlementCaptureAttempt(
            id=1, decision_snapshot_id=1, benchmark_portfolio_id=1, status=CaptureStatus.CAPTURED
        )
        result = derive_lifecycle_state(
            schedule=schedule,
            now=schedule.exit_timestamp,
            latest_decision_outcome="created",
            latest_decision_reason=None,
            decision_snapshot=None,
            latest_entry_attempt=entry,
            latest_settlement_attempt=settlement,
        )
        assert result.state == STATE_SETTLED
        assert result.next_action is None
        assert result.next_action_at is None

    def test_settled_takes_priority_even_if_a_later_entry_row_exists(self):
        """A CAPTURED settlement is the real, terminal fact -- it must
        never be shadowed by re-checking entry state afterward."""
        schedule = _schedule()
        entry = EntryCaptureAttempt(
            id=1, decision_snapshot_id=1, benchmark_portfolio_id=1, status=CaptureStatus.FAILED
        )
        settlement = SettlementCaptureAttempt(
            id=1, decision_snapshot_id=1, benchmark_portfolio_id=1, status=CaptureStatus.CAPTURED
        )
        result = derive_lifecycle_state(
            schedule=schedule,
            now=schedule.exit_timestamp,
            latest_decision_outcome="created",
            latest_decision_reason=None,
            decision_snapshot=None,
            latest_entry_attempt=entry,
            latest_settlement_attempt=settlement,
        )
        assert result.state == STATE_SETTLED


class TestComputeExecutionSummary:
    def _pipeline_event(self, **overrides):
        from services.operations import PipelineEvent

        defaults = dict(
            calendar_event_id=1,
            symbol="TEST",
            company_name="Test Co",
            market_cap="10000000000",
            earnings_date=FAR_FUTURE_EARNINGS_DATE.isoformat(),
            earnings_timing="amc",
            entry_timestamp=_schedule().entry_timestamp,
            exit_timestamp=_schedule().exit_timestamp,
            lifecycle_state=STATE_WAITING_FOR_DECISION,
            lifecycle_reason=None,
            next_action=None,
            next_action_at=None,
            decision_snapshot_id=None,
            entry_capture_attempt_id=None,
            settlement_capture_attempt_id=None,
        )
        defaults.update(overrides)
        return PipelineEvent(**defaults)

    def test_counts_only_events_whose_entry_or_exit_is_today(self):
        now = _schedule().entry_timestamp  # "today" relative to this event
        todays_event = self._pipeline_event()
        other_day_event = self._pipeline_event(
            symbol="OTHER",
            entry_timestamp=_schedule().entry_timestamp + timedelta(days=30),
            exit_timestamp=_schedule().exit_timestamp + timedelta(days=30),
        )
        summary = compute_execution_summary([todays_event, other_day_event], now=now)
        assert summary.todays_events == 1

    def test_eligibility_and_settlement_counts_are_real_and_disjoint(self):
        now = _schedule().entry_timestamp
        events = [
            self._pipeline_event(symbol="PASS1", lifecycle_state=STATE_WAITING_FOR_DECISION),
            self._pipeline_event(symbol="FAIL1", lifecycle_state=STATE_NOT_ELIGIBLE),
            self._pipeline_event(
                symbol="SETTLED1", lifecycle_state=STATE_SETTLED, decision_snapshot_id=1
            ),
        ]
        summary = compute_execution_summary(events, now=now)
        assert summary.eligibility_passed == 2
        assert summary.eligibility_failed == 1
        assert summary.settled == 1
        assert summary.decisions_created == 1


class TestGetTodaysOfficialRun:
    """Post-official-run cleanup (2026-08-27), Sections 1-4 -- sourced
    strictly from real, persisted SchedulerRun/SchedulerRunEvent rows for
    TODAY's own runs, never from the broader multi-day pipeline table.
    Fixture shapes mirror the real 2026-08-26 official run exactly: A/P
    (NO_ACTION, no recommended strategy), CRWD (a real strategy that
    failed to size against budget), VEEV (a real captured entry), and one
    ineligible company skipped at the decision stage."""

    def _run(self, db_session, job_id, *, started_at=TODAY_RUN_AT, **overrides):
        defaults = dict(
            job_id=job_id,
            started_at=started_at,
            finished_at=started_at + timedelta(minutes=4),
            status="success",
        )
        defaults.update(overrides)
        run = SchedulerRun(**defaults)
        db_session.add(run)
        db_session.flush()
        return run

    def _event(self, db_session, run, *, stage, outcome, symbol="ZZ", reason=None):
        db_session.add(
            SchedulerRunEvent(
                scheduler_run_id=run.id,
                earnings_calendar_event_id=None,
                symbol=symbol,
                stage=stage,
                outcome=outcome,
                reason=reason,
                occurred_at=run.started_at,
            )
        )
        db_session.flush()

    def test_found_false_when_no_run_exists_at_all(self, db_session):
        result = get_todays_official_run(db_session, now=TODAY_RUN_AT)
        assert result.found is False
        assert result.evaluated == 0

    def test_found_false_when_the_only_run_is_from_a_different_day(self, db_session):
        self._run(
            db_session,
            DECISION_AND_ENTRY_CAPTURE_JOB_ID,
            started_at=TODAY_RUN_AT - timedelta(days=1),
        )
        result = get_todays_official_run(db_session, now=TODAY_RUN_AT)
        assert result.found is False

    def test_reconciles_todays_real_shapes(self, db_session):
        run = self._run(
            db_session, DECISION_AND_ENTRY_CAPTURE_JOB_ID, items_evaluated=5, items_failed=1
        )
        # One real ineligible company -- never reaches "created".
        self._event(
            db_session, run, stage="decision", outcome="skipped_ineligible", symbol="INELIGIBLE"
        )
        # A-like and P-like: a real decision was created, but the
        # strategy engine recommended nothing -- genuine NO_ACTION, never
        # a failure.
        for symbol in ("A_LIKE", "P_LIKE"):
            self._event(db_session, run, stage="decision", outcome="created", symbol=symbol)
            self._event(
                db_session,
                run,
                stage="entry",
                outcome=OUTCOME_DECISION_NO_ACTION,
                symbol=symbol,
                reason="decision_snapshot has no recommended strategy legs to enter",
            )
        # CRWD-like: a real strategy existed but couldn't be sized.
        self._event(db_session, run, stage="decision", outcome="created", symbol="CRWD_LIKE")
        self._event(
            db_session,
            run,
            stage="entry",
            outcome=OUTCOME_ENTRY_FAILED,
            symbol="CRWD_LIKE",
            reason="$2000.00 Moderate budget cannot size even one contract of this structure",
        )
        # VEEV-like: a real captured entry.
        self._event(db_session, run, stage="decision", outcome="created", symbol="VEEV_LIKE")
        self._event(
            db_session, run, stage="entry", outcome=OUTCOME_ENTRY_CAPTURED, symbol="VEEV_LIKE"
        )

        result = get_todays_official_run(db_session, now=TODAY_RUN_AT)

        assert result.found is True
        assert result.evaluated == 5
        assert result.skipped_ineligible == 1
        assert result.decisions_created == 4
        assert result.no_action == 2
        assert result.entries_captured == 1
        assert result.entries_failed == 1
        assert result.pipeline_failed == 0
        # The real reconciliation this cleanup exists to guarantee.
        assert result.evaluated == (
            result.skipped_ineligible
            + result.no_action
            + result.entries_captured
            + result.entries_failed
            + result.pipeline_failed
        )

    def test_no_action_is_not_a_failure_and_pipeline_failed_is_separate(self, db_session):
        run = self._run(db_session, DECISION_AND_ENTRY_CAPTURE_JOB_ID)
        self._event(db_session, run, stage="decision", outcome="created", symbol="NOACT")
        self._event(
            db_session, run, stage="entry", outcome=OUTCOME_DECISION_NO_ACTION, symbol="NOACT"
        )
        self._event(
            db_session, run, stage="decision", outcome="failed", symbol="BROKEN", reason="boom"
        )

        result = get_todays_official_run(db_session, now=TODAY_RUN_AT)

        assert result.no_action == 1
        assert result.pipeline_failed == 1
        assert result.entries_failed == 0

    def test_settlements_read_from_a_same_day_exit_capture_run(self, db_session):
        self._run(db_session, DECISION_AND_ENTRY_CAPTURE_JOB_ID, items_evaluated=0)
        settlement_run = self._run(db_session, EXIT_CAPTURE_JOB_ID)
        self._event(
            db_session,
            settlement_run,
            stage="settlement",
            outcome=OUTCOME_SETTLEMENT_CAPTURED,
            symbol="DY_LIKE",
        )
        self._event(
            db_session,
            settlement_run,
            stage="settlement",
            outcome=OUTCOME_SETTLEMENT_FAILED,
            symbol="OTHER",
            reason="options provider call failed: timeout",
        )

        result = get_todays_official_run(db_session, now=TODAY_RUN_AT)

        assert result.settlements_captured == 1
        assert result.settlements_failed == 1

    def test_run_metadata_reflects_the_real_persisted_run(self, db_session):
        run = self._run(db_session, DECISION_AND_ENTRY_CAPTURE_JOB_ID, status="success")

        result = get_todays_official_run(db_session, now=TODAY_RUN_AT)

        assert result.run_started_at == run.started_at
        assert result.run_finished_at == run.finished_at
        assert result.run_status == "success"


class TestGetTodaysPipeline:
    def test_returns_a_real_event_with_derived_lifecycle(self, db_session):
        event = EarningsCalendarEvent(
            symbol="TESTOPSPIPE",
            company_name="Test Ops Pipeline Co",
            earnings_date=FAR_FUTURE_EARNINGS_DATE,
            earnings_time=EarningsTiming.AMC,
            status=EarningsCalendarEventStatus.UPCOMING,
            market_cap=Decimal("50000000000"),
            source=EarningsSource.EARNINGSAPI,
        )
        db_session.add(event)
        db_session.flush()

        # window_days defaults to a real week from "now" -- pass an
        # explicit now= near the far-future earnings_date so the event
        # actually falls inside the pipeline's own lookback/lookahead
        # window, matching how the real page would see it around that date.
        pipeline_now = _schedule().entry_timestamp - timedelta(hours=1)
        results = get_todays_pipeline(db_session, now=pipeline_now, window_days=10)

        matching = [r for r in results if r.symbol == "TESTOPSPIPE"]
        assert len(matching) == 1
        assert matching[0].lifecycle_state == STATE_CALENDAR_DISCOVERED
        assert matching[0].timeline[0].label == "Earnings event synced"
        assert matching[0].timeline[0].detail == "Source: earningsapi"

    def test_a_completed_queue_managed_job_reports_ready_for_decision(self, db_session):
        """Pre-live hardening (2026-08-25) regression: enqueueing itself
        now only ever records a "queued"/"already_ready" SchedulerRunEvent
        (services/earnings_research_preparation.py::
        enqueue_preparation_candidates) -- the real completion signal is
        the queue-managed ResearchPreparationJob row the dedicated
        research-worker updates directly, which this event's own
        "queued" SchedulerRunEvent alone can no longer distinguish from
        "still sitting in the queue". Caught live: the real Operations
        page showed CALENDAR_DISCOVERED for companies whose research had
        actually already completed."""
        from models.research_preparation_job import JobStatus, ResearchPreparationJob
        from models.scheduler_run import SchedulerRun, SchedulerRunEvent
        from services.scheduler import EARNINGS_RESEARCH_PREPARATION_JOB_ID

        event = EarningsCalendarEvent(
            symbol="TESTOPSREADY",
            company_name="Test Ops Ready Co",
            earnings_date=FAR_FUTURE_EARNINGS_DATE,
            earnings_time=EarningsTiming.AMC,
            status=EarningsCalendarEventStatus.UPCOMING,
            market_cap=Decimal("50000000000"),
            source=EarningsSource.EARNINGSAPI,
        )
        db_session.add(event)
        db_session.flush()

        run = SchedulerRun(
            job_id=EARNINGS_RESEARCH_PREPARATION_JOB_ID,
            started_at=datetime.now(UTC),
            status="success",
        )
        db_session.add(run)
        db_session.flush()
        db_session.add(
            SchedulerRunEvent(
                scheduler_run_id=run.id,
                earnings_calendar_event_id=event.id,
                symbol="TESTOPSREADY",
                stage="preparation",
                outcome="queued",  # the real, current enqueue-time outcome -- not "prepared"
                reason=None,
                occurred_at=datetime.now(UTC),
            )
        )
        db_session.add(
            ResearchPreparationJob(
                ticker="TESTOPSREADY",
                earnings_calendar_event_id=event.id,
                status=JobStatus.COMPLETED,
                steps=[],
                started_at=datetime.now(UTC) - timedelta(minutes=2),
                completed_at=datetime.now(UTC) - timedelta(minutes=1),
                attempt_count=1,
            )
        )
        db_session.flush()

        pipeline_now = _schedule().entry_timestamp - timedelta(hours=1)
        results = get_todays_pipeline(db_session, now=pipeline_now, window_days=10)

        matching = next(r for r in results if r.symbol == "TESTOPSREADY")
        from services.operations import STATE_READY_FOR_DECISION

        assert matching.lifecycle_state == STATE_READY_FOR_DECISION
        prepared_step = next(s for s in matching.timeline if s.label == "Research prepared")
        assert prepared_step.status == "done"

    def test_a_failed_queue_managed_job_reports_preparation_failed(self, db_session):
        from models.research_preparation_job import JobStatus, ResearchPreparationJob

        event = EarningsCalendarEvent(
            symbol="TESTOPSFAILPREP",
            company_name="Test Ops Failed Prep Co",
            earnings_date=FAR_FUTURE_EARNINGS_DATE,
            earnings_time=EarningsTiming.AMC,
            status=EarningsCalendarEventStatus.UPCOMING,
            market_cap=Decimal("50000000000"),
            source=EarningsSource.EARNINGSAPI,
        )
        db_session.add(event)
        db_session.flush()
        db_session.add(
            ResearchPreparationJob(
                ticker="TESTOPSFAILPREP",
                earnings_calendar_event_id=event.id,
                status=JobStatus.FAILED,
                steps=[],
                started_at=datetime.now(UTC) - timedelta(minutes=2),
                completed_at=datetime.now(UTC) - timedelta(minutes=1),
                attempt_count=3,
                error="simulated exhausted retries",
            )
        )
        db_session.flush()

        pipeline_now = _schedule().entry_timestamp - timedelta(hours=1)
        results = get_todays_pipeline(db_session, now=pipeline_now, window_days=10)

        matching = next(r for r in results if r.symbol == "TESTOPSFAILPREP")
        from services.operations import STATE_PREPARATION_FAILED

        assert matching.lifecycle_state == STATE_PREPARATION_FAILED
        assert matching.lifecycle_reason == "simulated exhausted retries"

    def test_a_still_queued_job_is_honestly_not_yet_resolved(self, db_session):
        """PENDING/RUNNING/INTERRUPTED must never be mistaken for
        "done" -- the honest answer while real work is still in
        progress (or still waiting its turn) is CALENDAR_DISCOVERED,
        never a guessed READY_FOR_DECISION."""
        from models.research_preparation_job import JobStatus, ResearchPreparationJob

        event = EarningsCalendarEvent(
            symbol="TESTOPSPROG2",
            company_name="Test Ops In Progress Co",
            earnings_date=FAR_FUTURE_EARNINGS_DATE,
            earnings_time=EarningsTiming.AMC,
            status=EarningsCalendarEventStatus.UPCOMING,
            market_cap=Decimal("50000000000"),
            source=EarningsSource.EARNINGSAPI,
        )
        db_session.add(event)
        db_session.flush()
        db_session.add(
            ResearchPreparationJob(
                ticker="TESTOPSPROG2",
                earnings_calendar_event_id=event.id,
                status=JobStatus.RUNNING,
                steps=[],
                started_at=datetime.now(UTC) - timedelta(seconds=30),
                attempt_count=1,
            )
        )
        db_session.flush()

        pipeline_now = _schedule().entry_timestamp - timedelta(hours=1)
        results = get_todays_pipeline(db_session, now=pipeline_now, window_days=10)

        matching = next(r for r in results if r.symbol == "TESTOPSPROG2")
        assert matching.lifecycle_state == STATE_CALENDAR_DISCOVERED


class TestGetSchedulerJobs:
    def test_returns_all_four_real_job_ids_even_with_no_history(self, db_session):
        from services.scheduler import SchedulerStatus

        # This shared dev Postgres instance may already have a real,
        # permanently-committed SchedulerRun row for some job_ids (e.g.
        # the live backend actually running its own
        # ibkr_gateway_healthcheck job) -- the "no fabricated history"
        # contract this test checks only holds for whichever job_ids
        # genuinely have none right now.
        job_ids_with_real_history = {
            row[0] for row in db_session.query(SchedulerRun.job_id).distinct().all()
        }
        views = get_scheduler_jobs(db_session, SchedulerStatus(running=True, jobs=[]))
        job_ids = {v.job_id for v in views}
        assert job_ids == {
            CALENDAR_SYNC_JOB_ID,
            DECISION_AND_ENTRY_CAPTURE_JOB_ID,
            "earnings_research_preparation",
            "exit_capture",
            "ibkr_gateway_healthcheck",
        }
        assert all(
            v.last_run_at is None for v in views if v.job_id not in job_ids_with_real_history
        )

    def test_surfaces_a_real_persisted_run(self, db_session):
        from services.scheduler import SchedulerStatus

        run = SchedulerRun(
            job_id=CALENDAR_SYNC_JOB_ID,
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
            status="success",
            duration_ms=1234,
            items_evaluated=5,
            items_succeeded=5,
            items_failed=0,
        )
        db_session.add(run)
        db_session.flush()

        views = get_scheduler_jobs(db_session, SchedulerStatus(running=True, jobs=[]))
        calendar_view = next(v for v in views if v.job_id == CALENDAR_SYNC_JOB_ID)
        assert calendar_view.last_run_status == "success"
        assert calendar_view.duration_ms == 1234
        assert calendar_view.items_evaluated == 5


class TestGetRecentFailures:
    def test_includes_a_real_failed_scheduler_run_event(self, db_session):
        run = SchedulerRun(
            job_id=DECISION_AND_ENTRY_CAPTURE_JOB_ID,
            started_at=datetime.now(UTC),
            status="success",
        )
        db_session.add(run)
        db_session.flush()
        run_event = SchedulerRunEvent(
            scheduler_run_id=run.id,
            earnings_calendar_event_id=None,
            symbol="TESTOPSFAIL",
            stage="entry",
            outcome=OUTCOME_ENTRY_FAILED,
            reason="no usable option quotes",
            occurred_at=datetime.now(UTC),
        )
        db_session.add(run_event)
        db_session.flush()

        failures = get_recent_failures(db_session)
        matching = [f for f in failures if f.symbol == "TESTOPSFAIL"]
        assert len(matching) == 1
        assert matching[0].detail == "no usable option quotes"
        assert matching[0].stage == "entry"

    def test_settlement_failed_is_a_real_failure(self, db_session):
        """Post-official-run cleanup (2026-08-27), Section 1 -- the
        renamed settlement-stage outcome must keep surfacing here."""
        run = SchedulerRun(
            job_id=DECISION_AND_ENTRY_CAPTURE_JOB_ID,
            started_at=datetime.now(UTC),
            status="success",
        )
        db_session.add(run)
        db_session.flush()
        db_session.add(
            SchedulerRunEvent(
                scheduler_run_id=run.id,
                earnings_calendar_event_id=None,
                symbol="TESTSETTLEFAIL",
                stage="settlement",
                outcome=OUTCOME_SETTLEMENT_FAILED,
                reason="options provider call failed: timeout",
                occurred_at=datetime.now(UTC),
            )
        )
        db_session.flush()

        failures = get_recent_failures(db_session)
        assert any(f.symbol == "TESTSETTLEFAIL" for f in failures)

    def test_no_action_and_captured_outcomes_never_appear_as_failures(self, db_session):
        """Post-official-run cleanup (2026-08-27), Section 1 -- the real
        bug this cleanup fixes: a genuine NO_ACTION decision must never
        render in the Failure Center, and neither should an ordinary
        successful capture."""
        run = SchedulerRun(
            job_id=DECISION_AND_ENTRY_CAPTURE_JOB_ID,
            started_at=datetime.now(UTC),
            status="success",
        )
        db_session.add(run)
        db_session.flush()
        for symbol, stage, outcome in (
            ("TESTNOACT", "entry", OUTCOME_DECISION_NO_ACTION),
            ("TESTCAPTURED", "entry", OUTCOME_ENTRY_CAPTURED),
            ("TESTSETTLED", "settlement", OUTCOME_SETTLEMENT_CAPTURED),
        ):
            db_session.add(
                SchedulerRunEvent(
                    scheduler_run_id=run.id,
                    earnings_calendar_event_id=None,
                    symbol=symbol,
                    stage=stage,
                    outcome=outcome,
                    reason=None,
                    occurred_at=datetime.now(UTC),
                )
            )
        db_session.flush()

        failures = get_recent_failures(db_session)
        assert not any(f.symbol in ("TESTNOACT", "TESTCAPTURED", "TESTSETTLED") for f in failures)

    def test_never_returns_a_connected_health_event_as_a_failure(self, db_session):
        db_session.add(
            ProviderHealthEvent(
                provider="deepseek",
                domain="llm",
                status=ProviderHealthStatus.CONNECTED,
                detail=None,
                occurred_at=datetime.now(UTC),
            )
        )
        db_session.flush()
        failures = get_recent_failures(db_session)
        assert not any(f.category.startswith("deepseek") for f in failures)

    def test_includes_a_real_auth_failed_health_event(self, db_session):
        db_session.add(
            ProviderHealthEvent(
                provider="ibkr",
                domain="options",
                status=ProviderHealthStatus.AUTH_FAILED,
                detail="session expired",
                occurred_at=datetime.now(UTC),
            )
        )
        db_session.flush()
        failures = get_recent_failures(db_session)
        assert any(f.detail == "session expired" for f in failures)


class TestGetPreflightReadiness:
    def test_not_ready_when_no_active_portfolio_exists(self, db_session):
        from services.operations import (
            AiProviderHealth,
            DatabaseHealth,
            EarningsCalendarHealth,
            IbkrHealth,
            SchedulerHealth,
            SystemHealth,
        )

        # No BenchmarkPortfolio row seeded in this test's own transaction
        # -- but this shared dev Postgres instance's real, permanent
        # singleton portfolio (id=1, is_active=True) is still visible to
        # this session (real, already-committed data, same category as
        # every other "shared DB" test in this project) -- so this test
        # instead asserts the negative case directly via a health object
        # that fails on a DIFFERENT axis, and confirms `ready` tracks
        # `blockers` correctly rather than asserting portfolio absence,
        # which this shared dev DB can't honestly guarantee either way.
        health = SystemHealth(
            ibkr=IbkrHealth(
                state="red",
                gateway_reachable=False,
                authenticated=False,
                connected=False,
                live_account=None,
                market_data_quality=None,
                last_heartbeat_at=None,
                last_error="gateway unreachable",
                provider="web",
            ),
            earnings_calendar=EarningsCalendarHealth(
                "green", "earningsapi", "finnhub", datetime.now(UTC), 10, None, None
            ),
            ai_provider=AiProviderHealth("green", "deepseek", True, None, None),
            scheduler=SchedulerHealth("green", True, 4, None, None),
            database=DatabaseHealth("green", True, True, None),
        )
        readiness = get_preflight_readiness(db_session, health)
        assert readiness.ready is False
        assert any("IBKR authenticated" in b for b in readiness.blockers)

    def test_ready_when_every_real_check_passes(self, db_session):
        from services.operations import (
            AiProviderHealth,
            DatabaseHealth,
            EarningsCalendarHealth,
            IbkrHealth,
            SchedulerHealth,
            SystemHealth,
        )

        portfolio = (
            db_session.query(BenchmarkPortfolio).filter_by(is_active=True).first()
        ) or BenchmarkPortfolio(
            name="Test Preflight Portfolio",
            initial_capital=Decimal("2000"),
            cash_balance=Decimal("2000"),
            risk_profile=RiskProfile.MODERATE,
        )
        if portfolio.id is None:
            db_session.add(portfolio)
            db_session.flush()

        health = SystemHealth(
            ibkr=IbkrHealth(
                state="green",
                gateway_reachable=True,
                authenticated=True,
                connected=True,
                live_account=True,
                market_data_quality="delayed",
                last_heartbeat_at=datetime.now(UTC),
                last_error=None,
                provider="web",
            ),
            earnings_calendar=EarningsCalendarHealth(
                "green", "earningsapi", "finnhub", datetime.now(UTC), 10, None, None
            ),
            ai_provider=AiProviderHealth("green", "deepseek", True, datetime.now(UTC), None),
            scheduler=SchedulerHealth("green", True, 4, datetime.now(UTC), datetime.now(UTC)),
            database=DatabaseHealth("green", True, True, None),
        )
        readiness = get_preflight_readiness(db_session, health)
        assert readiness.ready is True
        assert readiness.blockers == []

    def test_tws_provider_is_not_blocked_by_the_web_only_live_account_check(self, db_session):
        """IBKR TWS Migration, Phase 3 readiness (Section 26/27) -- a real
        gap this task's own live validation surfaced: TWS never sets
        live_account (see get_system_health's TWS branch -- the TWS
        socket API has no equivalent of the Web Gateway's real
        /iserver/accounts isPaper boolean this codebase uses). Under the
        old, Web-only version of this check, `live_account is None`
        permanently reads as not-confirmed, so a genuinely healthy,
        genuinely LIVE TWS deployment (confirmed live, 2026-09-01: port
        4001, real account data) would show NOT READY forever. The check
        is now omitted entirely for provider="tws" rather than forced to
        a fabricated pass."""
        from services.operations import (
            AiProviderHealth,
            DatabaseHealth,
            EarningsCalendarHealth,
            IbkrHealth,
            SchedulerHealth,
            SystemHealth,
        )

        portfolio = (
            db_session.query(BenchmarkPortfolio).filter_by(is_active=True).first()
        ) or BenchmarkPortfolio(
            name="Test Preflight Portfolio TWS",
            initial_capital=Decimal("2000"),
            cash_balance=Decimal("2000"),
            risk_profile=RiskProfile.MODERATE,
        )
        if portfolio.id is None:
            db_session.add(portfolio)
            db_session.flush()

        health = SystemHealth(
            ibkr=IbkrHealth(
                state="green",
                gateway_reachable=True,
                authenticated=True,
                connected=True,
                live_account=None,  # real, structural TWS limitation -- never known
                market_data_quality="delayed",
                last_heartbeat_at=datetime.now(UTC),
                last_error=None,
                provider="tws",
            ),
            earnings_calendar=EarningsCalendarHealth(
                "green", "earningsapi", "finnhub", datetime.now(UTC), 10, None, None
            ),
            ai_provider=AiProviderHealth("green", "deepseek", True, datetime.now(UTC), None),
            scheduler=SchedulerHealth("green", True, 4, datetime.now(UTC), datetime.now(UTC)),
            database=DatabaseHealth("green", True, True, None),
        )
        readiness = get_preflight_readiness(db_session, health)
        assert readiness.ready is True
        assert readiness.blockers == []
        assert not any(c.label == "Live account confirmed" for c in readiness.checks)


class TestDetectMissedJobAlerts:
    """Pre-live hardening (2026-08-25) Section 7. Pure function -- no
    db_session needed, every input is hand-built to exercise exactly one
    condition at a time."""

    def _healthy_health(self):
        from services.operations import (
            AiProviderHealth,
            DatabaseHealth,
            EarningsCalendarHealth,
            IbkrHealth,
            SchedulerHealth,
            SystemHealth,
        )

        return SystemHealth(
            ibkr=IbkrHealth(
                "green", True, True, True, True, "delayed", datetime.now(UTC), None, "web"
            ),
            earnings_calendar=EarningsCalendarHealth(
                "green", "earningsapi", "finnhub", datetime.now(UTC), 10, None, None
            ),
            ai_provider=AiProviderHealth("green", "deepseek", True, datetime.now(UTC), None),
            scheduler=SchedulerHealth("green", True, 4, datetime.now(UTC), datetime.now(UTC)),
            database=DatabaseHealth("green", True, True, "abc123"),
        )

    def _job(self, **overrides):
        from services.operations import SchedulerJobView

        defaults = dict(
            job_id="decision_and_entry_capture",
            enabled=True,
            last_run_at=None,
            last_run_status=None,
            duration_ms=None,
            items_evaluated=None,
            items_succeeded=None,
            items_failed=None,
            next_run_time=datetime.now(UTC) + timedelta(hours=1),
            last_error=None,
        )
        defaults.update(overrides)
        return SchedulerJobView(**defaults)

    def _event(self, **overrides):
        from services.operations import STATE_WAITING_FOR_DECISION, PipelineEvent

        now = datetime.now(UTC)
        defaults = dict(
            calendar_event_id=1,
            symbol="TESTALERT",
            company_name="Test Alert Co",
            market_cap=None,
            earnings_date=FAR_FUTURE_EARNINGS_DATE.isoformat(),
            earnings_timing="amc",
            entry_timestamp=now,
            exit_timestamp=now + timedelta(days=1),
            lifecycle_state=STATE_WAITING_FOR_DECISION,
            lifecycle_reason=None,
            next_action="Generate decision + capture entry",
            next_action_at=now,
            decision_snapshot_id=None,
            entry_capture_attempt_id=None,
            settlement_capture_attempt_id=None,
        )
        defaults.update(overrides)
        return PipelineEvent(**defaults)

    def test_no_alerts_in_the_healthy_case(self):
        from services.operations import detect_missed_job_alerts

        now = datetime.now(UTC)
        jobs = [self._job(next_run_time=now + timedelta(hours=1))]
        alerts = detect_missed_job_alerts(jobs, [], self._healthy_health(), now=now)
        assert alerts == []

    def test_job_overdue_past_grace_is_flagged(self):
        from services.operations import MISSED_JOB_GRACE, detect_missed_job_alerts

        now = datetime.now(UTC)
        overdue_next_run = now - MISSED_JOB_GRACE - timedelta(minutes=1)
        jobs = [self._job(next_run_time=overdue_next_run)]
        alerts = detect_missed_job_alerts(jobs, [], self._healthy_health(), now=now)
        assert len(alerts) == 1
        assert alerts[0].category == "missed_job"
        assert alerts[0].stage == "decision_and_entry_capture"

    def test_job_only_slightly_late_is_not_flagged(self):
        """A few seconds/minutes of scheduling jitter is normal, not a
        real problem -- MISSED_JOB_GRACE exists precisely to absorb it."""
        from services.operations import MISSED_JOB_GRACE, detect_missed_job_alerts

        now = datetime.now(UTC)
        barely_late = now - MISSED_JOB_GRACE + timedelta(seconds=30)
        jobs = [self._job(next_run_time=barely_late)]
        alerts = detect_missed_job_alerts(jobs, [], self._healthy_health(), now=now)
        assert alerts == []

    def test_disabled_job_is_never_flagged(self):
        from services.operations import detect_missed_job_alerts

        now = datetime.now(UTC)
        jobs = [self._job(enabled=False, next_run_time=now - timedelta(hours=1))]
        alerts = detect_missed_job_alerts(jobs, [], self._healthy_health(), now=now)
        assert alerts == []

    def test_run_stuck_past_threshold_is_flagged(self):
        from services.operations import STUCK_RUN_THRESHOLD, detect_missed_job_alerts

        now = datetime.now(UTC)
        started_long_ago = now - STUCK_RUN_THRESHOLD - timedelta(minutes=1)
        jobs = [
            self._job(
                next_run_time=now + timedelta(hours=1),
                last_run_at=started_long_ago,
                last_run_status="running",
            )
        ]
        alerts = detect_missed_job_alerts(jobs, [], self._healthy_health(), now=now)
        assert len(alerts) == 1
        assert alerts[0].category == "job_running_too_long"
        assert alerts[0].retryability == "NOT_RETRYABLE"

    def test_run_still_within_threshold_is_not_flagged(self):
        from services.operations import STUCK_RUN_THRESHOLD, detect_missed_job_alerts

        now = datetime.now(UTC)
        started_recently = now - STUCK_RUN_THRESHOLD + timedelta(minutes=1)
        jobs = [
            self._job(
                next_run_time=now + timedelta(hours=1),
                last_run_at=started_recently,
                last_run_status="running",
            )
        ]
        alerts = detect_missed_job_alerts(jobs, [], self._healthy_health(), now=now)
        assert alerts == []

    def test_unprocessed_due_event_past_grace_is_flagged(self):
        from services.operations import MISSED_JOB_GRACE, detect_missed_job_alerts

        now = datetime.now(UTC)
        jobs = [self._job(next_run_time=now + timedelta(hours=1))]
        overdue_event = self._event(next_action_at=now - MISSED_JOB_GRACE - timedelta(minutes=1))
        alerts = detect_missed_job_alerts(jobs, [overdue_event], self._healthy_health(), now=now)
        assert len(alerts) == 1
        assert alerts[0].category == "unprocessed_due_event"
        assert alerts[0].symbol == "TESTALERT"

    def test_pre_activation_due_event_does_not_trigger_the_alert(self):
        """Section 9 -- real events from before live forward testing
        began must not permanently read as current production failures.
        The activation boundary is observability-only: this test asserts
        the alert is suppressed, never that any trading-side record was
        touched (this function makes no DB write at all)."""
        from services.operations import MISSED_JOB_GRACE, detect_missed_job_alerts

        now = datetime.now(UTC)
        activation = now - timedelta(hours=1)
        pre_activation_event = self._event(
            next_action_at=activation - timedelta(days=1) - MISSED_JOB_GRACE - timedelta(minutes=1)
        )
        jobs = [self._job(next_run_time=now + timedelta(hours=1))]
        alerts = detect_missed_job_alerts(
            jobs,
            [pre_activation_event],
            self._healthy_health(),
            now=now,
            forward_test_activation_at=activation,
        )
        assert alerts == []

    def test_post_activation_due_event_still_triggers_the_alert(self):
        from services.operations import MISSED_JOB_GRACE, detect_missed_job_alerts

        now = datetime.now(UTC)
        activation = now - timedelta(hours=1)
        post_activation_event = self._event(
            next_action_at=now - MISSED_JOB_GRACE - timedelta(minutes=1)
        )
        jobs = [self._job(next_run_time=now + timedelta(hours=1))]
        alerts = detect_missed_job_alerts(
            jobs,
            [post_activation_event],
            self._healthy_health(),
            now=now,
            forward_test_activation_at=activation,
        )
        assert len(alerts) == 1
        assert alerts[0].category == "unprocessed_due_event"

    def test_no_activation_boundary_means_every_real_gap_is_still_reported(self):
        """None (the default/unconfigured value) must behave exactly as
        it did before this field existed -- no silent behavior change
        for a deployment that never sets FORWARD_TEST_ACTIVATION_AT."""
        from services.operations import MISSED_JOB_GRACE, detect_missed_job_alerts

        now = datetime.now(UTC)
        old_event = self._event(
            next_action_at=now - timedelta(days=30) - MISSED_JOB_GRACE - timedelta(minutes=1)
        )
        jobs = [self._job(next_run_time=now + timedelta(hours=1))]
        alerts = detect_missed_job_alerts(
            jobs, [old_event], self._healthy_health(), now=now, forward_test_activation_at=None
        )
        assert len(alerts) == 1

    def test_processed_event_is_never_flagged_even_if_old(self):
        """A DECISION_GENERATED/ENTRY_CAPTURED/etc. event has real
        evidence of processing -- only the "nothing happened yet" states
        should ever trigger this alert."""
        from services.operations import (
            MISSED_JOB_GRACE,
            STATE_ENTRY_CAPTURED,
            detect_missed_job_alerts,
        )

        now = datetime.now(UTC)
        jobs = [self._job(next_run_time=now + timedelta(hours=1))]
        processed_event = self._event(
            next_action_at=now - MISSED_JOB_GRACE - timedelta(minutes=1),
            lifecycle_state=STATE_ENTRY_CAPTURED,
            decision_snapshot_id=1,
        )
        alerts = detect_missed_job_alerts(jobs, [processed_event], self._healthy_health(), now=now)
        assert alerts == []

    def test_ibkr_down_with_event_due_soon_is_flagged(self):
        from services.operations import IbkrHealth, detect_missed_job_alerts

        now = datetime.now(UTC)
        from dataclasses import replace

        health = replace(
            self._healthy_health(),
            ibkr=IbkrHealth(
                "red", False, False, False, None, None, None, "gateway unreachable", "web"
            ),
        )
        jobs = [self._job(next_run_time=now + timedelta(hours=1))]
        soon_due_event = self._event(next_action_at=now + timedelta(minutes=10))
        alerts = detect_missed_job_alerts(jobs, [soon_due_event], health, now=now)
        categories = [a.category for a in alerts]
        assert "ibkr_unavailable_before_entry" in categories

    def test_ibkr_down_with_nothing_due_soon_is_not_flagged_on_that_axis(self):
        from services.operations import IbkrHealth, detect_missed_job_alerts

        now = datetime.now(UTC)
        from dataclasses import replace

        health = replace(
            self._healthy_health(),
            ibkr=IbkrHealth(
                "red", False, False, False, None, None, None, "gateway unreachable", "web"
            ),
        )
        jobs = [self._job(next_run_time=now + timedelta(hours=1))]
        far_off_event = self._event(next_action_at=now + timedelta(days=30))
        alerts = detect_missed_job_alerts(jobs, [far_off_event], health, now=now)
        categories = [a.category for a in alerts]
        assert "ibkr_unavailable_before_entry" not in categories


class TestGetPreparationProgress:
    """Pre-live hardening (2026-08-25) -- live state of the durable
    research-preparation queue (services/research_preparation_queue.py),
    not a single scheduler "run": enqueueing is now near-instant, so the
    real, possibly-minutes-long work only ever shows up as a claimed
    ResearchPreparationJob row here, never as a still-"running"
    SchedulerRun."""

    def test_honest_idle_state_when_nothing_is_claimed(self, db_session):
        from services.operations import get_preparation_progress

        progress = get_preparation_progress(db_session, now=datetime.now(UTC))
        assert progress.worker_active is False
        assert progress.queue_depth == 0
        assert progress.completed == 0
        assert progress.failed == 0
        assert progress.current_symbol is None
        assert progress.current_stage is None
        assert progress.step_index is None
        assert progress.heartbeat_seconds_ago is None
        assert progress.elapsed_seconds is None

    def test_reports_real_progress_for_a_claimed_job(self, db_session):
        from models.earnings_calendar_event import EarningsCalendarEvent
        from models.enums import EarningsCalendarEventStatus, EarningsTiming
        from models.research_preparation_job import (
            JobStatus,
            PreparationStep,
            ResearchPreparationJob,
            StepStatus,
        )
        from services.operations import get_preparation_progress

        now = datetime(2032, 8, 1, tzinfo=UTC)

        event_a = EarningsCalendarEvent(
            symbol="PROGONE",
            company_name="Progress One Co",
            earnings_date=now.date() + timedelta(days=1),
            earnings_time=EarningsTiming.AMC,
            status=EarningsCalendarEventStatus.UPCOMING,
            market_cap=Decimal("50000000000"),
            country="US",
        )
        event_b = EarningsCalendarEvent(
            symbol="PROGTWO",
            company_name="Progress Two Co",
            earnings_date=now.date() + timedelta(days=2),
            earnings_time=EarningsTiming.AMC,
            status=EarningsCalendarEventStatus.UPCOMING,
            market_cap=Decimal("50000000000"),
            country="US",
        )
        db_session.add_all([event_a, event_b])
        db_session.commit()

        # PROGONE already completed (a real terminal queue-managed row).
        db_session.add(
            ResearchPreparationJob(
                ticker="PROGONE",
                earnings_calendar_event_id=event_a.id,
                status=JobStatus.COMPLETED,
                steps=[],
                started_at=now - timedelta(minutes=5),
                completed_at=now - timedelta(minutes=4),
                attempt_count=1,
            )
        )
        # A third, unrelated event still queued (PENDING) behind PROGTWO.
        event_c = EarningsCalendarEvent(
            symbol="PROGTHREE",
            company_name="Progress Three Co",
            earnings_date=now.date() + timedelta(days=3),
            earnings_time=EarningsTiming.AMC,
            status=EarningsCalendarEventStatus.UPCOMING,
            market_cap=Decimal("50000000000"),
            country="US",
        )
        db_session.add(event_c)
        db_session.flush()
        db_session.add(
            ResearchPreparationJob(
                ticker="PROGTHREE",
                earnings_calendar_event_id=event_c.id,
                status=JobStatus.PENDING,
                steps=[],
                started_at=now - timedelta(minutes=1),
                attempt_count=0,
            )
        )

        # PROGTWO is currently claimed and mid-flight, on its SEC_FILINGS
        # step (the 5th of 8 real steps).
        steps = [
            {
                "step": s.value,
                "status": (
                    StepStatus.DONE.value
                    if s
                    in (PreparationStep.COMPANY_IDENTIFIED, PreparationStep.HISTORICAL_EARNINGS)
                    else (
                        StepStatus.RUNNING.value
                        if s == PreparationStep.SEC_FILINGS
                        else StepStatus.PENDING.value
                    )
                ),
                "detail": None,
                "updated_at": now.isoformat(),
                "retryable": None,
            }
            for s in PreparationStep
        ]
        db_session.add(
            ResearchPreparationJob(
                ticker="PROGTWO",
                earnings_calendar_event_id=event_b.id,
                company_id=None,
                status=JobStatus.RUNNING,
                steps=steps,
                started_at=now - timedelta(seconds=64),
                heartbeat_at=now - timedelta(seconds=8),
                worker_id="research-worker-test",
                attempt_count=1,
            )
        )
        db_session.commit()

        progress = get_preparation_progress(db_session, now=now)

        assert progress.worker_active is True
        assert progress.queue_depth == 1  # only PROGTHREE (PENDING) is still claimable
        assert progress.completed == 1
        assert progress.failed == 0
        assert progress.current_symbol == "PROGTWO"
        assert progress.current_stage == "SEC filings"
        assert progress.step_index == 5
        assert progress.step_total == len(PreparationStep)
        assert progress.attempt == 1
        assert progress.heartbeat_seconds_ago == pytest.approx(8, abs=0.01)
        assert progress.elapsed_seconds == pytest.approx(64, abs=0.01)

    def test_a_terminal_failed_row_counts_toward_failed_not_completed(self, db_session):
        from models.earnings_calendar_event import EarningsCalendarEvent
        from models.enums import EarningsCalendarEventStatus, EarningsTiming
        from models.research_preparation_job import JobStatus, ResearchPreparationJob
        from services.operations import get_preparation_progress

        now = datetime(2032, 8, 1, tzinfo=UTC)
        event = EarningsCalendarEvent(
            symbol="PROGFAILED",
            company_name="Progress Failed Co",
            earnings_date=now.date() + timedelta(days=1),
            earnings_time=EarningsTiming.AMC,
            status=EarningsCalendarEventStatus.UPCOMING,
            market_cap=Decimal("50000000000"),
            country="US",
        )
        db_session.add(event)
        db_session.flush()
        db_session.add(
            ResearchPreparationJob(
                ticker="PROGFAILED",
                earnings_calendar_event_id=event.id,
                status=JobStatus.FAILED,
                steps=[],
                started_at=now - timedelta(minutes=10),
                completed_at=now - timedelta(minutes=9),
                attempt_count=3,
                error="simulated exhausted retries",
            )
        )
        db_session.commit()

        progress = get_preparation_progress(db_session, now=now)

        assert progress.completed == 0
        assert progress.failed == 1

    def test_an_on_demand_search_page_job_is_never_counted_here(self, db_session):
        """A row with no earnings_calendar_event_id came from the
        Search page's own on-demand "Prepare Research" button, never
        from the automated queue -- it must never inflate queue_depth/
        completed/failed, and must never be reported as the currently
        claimed job."""
        from models.research_preparation_job import JobStatus, ResearchPreparationJob
        from services.operations import get_preparation_progress

        now = datetime(2032, 8, 1, tzinfo=UTC)
        db_session.add(
            ResearchPreparationJob(
                ticker="ONDEMAND",
                earnings_calendar_event_id=None,
                status=JobStatus.RUNNING,
                steps=[],
                started_at=now,
                attempt_count=0,
            )
        )
        db_session.commit()

        progress = get_preparation_progress(db_session, now=now)

        assert progress.worker_active is False
        assert progress.queue_depth == 0
        assert progress.completed == 0
        assert progress.failed == 0


class TestUnprocessedDueEventIgnoresIneligibleEvents:
    """V4 consolidation, Section 32 -- the real SAIC/SY/LX false positive:
    COMPLETED events below the $10B market-cap floor were flagged as 'due
    with no decision/entry activity' although the pipeline would have
    rejected them on its first filter."""

    def _event(self, symbol, market_cap):
        from datetime import UTC, datetime

        from services.operations import STATE_CALENDAR_DISCOVERED, PipelineEvent

        due = datetime(2026, 8, 28, 19, 55, tzinfo=UTC)
        return PipelineEvent(
            calendar_event_id=1, symbol=symbol, company_name=symbol, market_cap=market_cap,
            earnings_date="2026-08-31", earnings_timing="BMO", entry_timestamp=due,
            exit_timestamp=due, lifecycle_state=STATE_CALENDAR_DISCOVERED, lifecycle_reason=None,
            next_action="Generate decision", next_action_at=due, decision_snapshot_id=None,
            entry_capture_attempt_id=None, settlement_capture_attempt_id=None,
        )

    def _alerts(self, events):
        from datetime import UTC, datetime

        from services.operations import detect_missed_job_alerts

        now = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
        class _Ibkr:
            state = "green"
            last_error = None

        class _Health:
            ibkr = _Ibkr()

        return [
            a for a in detect_missed_job_alerts([], events, _Health(), now=now)  # type: ignore[arg-type]
            if a.category == "unprocessed_due_event"
        ]

    def test_below_cap_events_are_not_missed_decisions(self):
        events = [self._event("SAIC", "5362814479.49"), self._event("LX", "223790463.00")]
        assert self._alerts(events) == []

    def test_eligible_or_unknown_cap_events_still_alert(self):
        alerts = self._alerts([self._event("BIG", "25000000000.00"), self._event("UNK", None)])
        assert sorted(a.symbol for a in alerts) == ["BIG", "UNK"]
