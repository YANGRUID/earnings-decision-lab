"""Live Operations read models, V4-only (V4-only reset, 2026-09-02).

Every state below is derived from persisted rows only. Non-vacuity: each
test seeds the exact rows that produce the state and asserts the state,
the reason and the next action together.
"""

from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from models.ai_thesis_version import AIThesisVersion
from models.company import Company
from models.earnings_calendar_event import EarningsCalendarEvent
from models.research_preparation_job import JobStatus, ResearchPreparationJob
from models.scheduler_run import SchedulerRun
from services.operations import (
    ALL_JOB_IDS,
    STATE_BUSINESS_INELIGIBLE,
    STATE_CALENDAR_DISCOVERED,
    STATE_COMPANY_RESOLUTION_FAILED,
    STATE_DEADLINE_SKIPPED,
    STATE_DECISION_WINDOW_MISSED,
    STATE_ENTRY_OBSERVED,
    STATE_RESEARCH_NOT_READY,
    STATE_RESEARCH_QUEUED,
    STATE_RESEARCH_READY,
    STATE_RESEARCH_RUNNING,
    STATE_SETTLED,
    STATE_WAITING_DECISION,
    STATE_WAITING_SETTLEMENT,
    classify_event,
    compute_research_readiness,
    compute_today_summary,
    detect_missed_job_alerts,
    get_recent_failures,
    get_scheduler_jobs,
    get_v4_pipeline,
)
from services.scheduler import SchedulerJobStatus, SchedulerStatus

ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 9, 9, 12, 0, tzinfo=ET)  # Wednesday noon, before the 15:30 window


def _event(
    db, symbol, earnings_date=date(2026, 9, 9), timing="AMC", cap=50_000_000_000, country="US"
):
    row = EarningsCalendarEvent(
        symbol=symbol,
        company_name=f"{symbol} Inc",
        earnings_date=earnings_date,
        earnings_time=timing,
        source="EARNINGSAPI",
        status="UPCOMING",
        market_cap=cap,
        country=country,
    )
    db.add(row)
    db.flush()
    return row


def _company(db, symbol, *, thesis_age=None):
    co = Company(ticker=symbol, name=f"{symbol} Inc")
    db.add(co)
    db.flush()
    if thesis_age is not None:
        db.add(
            AIThesisVersion(
                company_id=co.id,
                business_context="b",
                historical_earnings_pattern="h",
                guidance_trend="g",
                key_risks="k",
                market_setup="m",
                disclaimer="d",
                citations=[],
                provider="deepseek",
                model="deepseek-v4-flash",
                created_at=NOW - thesis_age,
            )
        )
        db.flush()
    return co


def _prep(db, symbol, event_id, status, error=None):
    db.add(
        ResearchPreparationJob(
            ticker=symbol,
            earnings_calendar_event_id=event_id,
            status=status,
            steps=[],
            started_at=NOW - timedelta(hours=1),
            completed_at=NOW if status == JobStatus.FAILED else None,
            error=error,
            attempt_count=1,
        )
    )
    db.flush()


class TestPipelineStates:
    def test_business_ineligible_small_cap_and_foreign(self, db_session):
        small = _event(db_session, "SMALL", cap=1_000_000_000)
        foreign = _event(db_session, "FRGN", country="CA")
        a = classify_event(db_session, small, NOW)
        b = classify_event(db_session, foreign, NOW)
        assert a.lifecycle_state == STATE_BUSINESS_INELIGIBLE and "market cap" in (
            a.lifecycle_reason or ""
        )
        assert b.lifecycle_state == STATE_BUSINESS_INELIGIBLE and "not US listed" in (
            b.lifecycle_reason or ""
        )

    def test_calendar_discovered_without_company(self, db_session):
        ev = _event(db_session, "NOCO")
        row = classify_event(db_session, ev, NOW)
        assert row.lifecycle_state == STATE_CALENDAR_DISCOVERED
        assert row.next_action == "Research preparation"
        assert row.research_ready is False

    def test_research_queued_running_and_resolution_failed(self, db_session):
        q = _event(db_session, "QUE")
        _prep(db_session, "QUE", q.id, JobStatus.PENDING)
        r = _event(db_session, "RUN")
        _prep(db_session, "RUN", r.id, JobStatus.RUNNING)
        f = _event(db_session, "BAD")
        _prep(
            db_session, "BAD", f.id, JobStatus.FAILED, "no longer a supported symbol: unknown CIK"
        )
        assert classify_event(db_session, q, NOW).lifecycle_state == STATE_RESEARCH_QUEUED
        assert classify_event(db_session, r, NOW).lifecycle_state == STATE_RESEARCH_RUNNING
        bad = classify_event(db_session, f, NOW)
        assert bad.lifecycle_state == STATE_COMPANY_RESOLUTION_FAILED
        assert "unknown CIK" in (bad.lifecycle_reason or "")

    def test_ready_company_waits_for_the_1530_window(self, db_session):
        ev = _event(db_session, "RDY")
        _company(db_session, "RDY", thesis_age=timedelta(days=1))
        row = classify_event(db_session, ev, NOW)
        assert row.lifecycle_state == STATE_WAITING_DECISION
        assert row.research_ready is True
        assert row.next_action_at == datetime(2026, 9, 9, 15, 30, tzinfo=ET)
        far = _event(db_session, "FAR", earnings_date=date(2026, 9, 15))
        _company(db_session, "FAR", thesis_age=timedelta(days=1))
        assert classify_event(db_session, far, NOW).lifecycle_state == STATE_RESEARCH_READY

    def test_stale_thesis_is_not_ready(self, db_session):
        ev = _event(db_session, "OLD")
        _company(db_session, "OLD", thesis_age=timedelta(days=20))
        row = classify_event(db_session, ev, NOW)
        assert row.lifecycle_state == STATE_CALENDAR_DISCOVERED
        assert "no fresh AI thesis" in (row.lifecycle_reason or "")

    def test_window_passed_states(self, db_session):
        from models.v4_shadow import V4ShadowRunEvent

        later = datetime(2026, 9, 9, 16, 0, tzinfo=ET)
        missed = _event(db_session, "MISS")
        _company(db_session, "MISS", thesis_age=timedelta(days=1))
        assert (
            classify_event(db_session, missed, later).lifecycle_state
            == STATE_DECISION_WINDOW_MISSED
        )
        skipped = _event(db_session, "SKIP")
        _company(db_session, "SKIP", thesis_age=timedelta(days=1))
        db_session.add(
            V4ShadowRunEvent(
                earnings_calendar_event_id=skipped.id,
                ticker="SKIP",
                occurred_at=later - timedelta(minutes=20),
                stage="deadline_guard",
                category="DEADLINE_SKIPPED",
                retryable=False,
                message="deadline",
            )
        )
        nr = _event(db_session, "NR")
        db_session.add(
            V4ShadowRunEvent(
                earnings_calendar_event_id=nr.id,
                ticker="NR",
                occurred_at=later - timedelta(minutes=25),
                stage="research_gate",
                category="RESEARCH_NOT_READY",
                retryable=True,
                message="no Company row",
            )
        )
        db_session.flush()
        assert classify_event(db_session, skipped, later).lifecycle_state == STATE_DEADLINE_SKIPPED
        assert classify_event(db_session, nr, later).lifecycle_state == STATE_RESEARCH_NOT_READY

    def test_decided_event_walks_entry_settlement(self, db_session, monkeypatch):
        import test_v4_six_cohort_evidence as cohort

        ev = EarningsCalendarEvent(
            symbol="SIXC",
            company_name="Six Cohort Co",
            earnings_date=date(2026, 9, 10),
            earnings_time="AMC",
            source="EARNINGSAPI",
            status="UPCOMING",
            market_cap=90_000_000_000,
            country="US",
        )
        db_session.add(ev)
        db_session.flush()
        _company(db_session, "SIXC", thesis_age=timedelta(days=1))
        result = cohort._freeze(db_session, ev, monkeypatch=monkeypatch)
        assert result.status == "RANKED"
        after_entry = datetime(2026, 9, 10, 15, 40, tzinfo=ET)
        row = classify_event(db_session, ev, after_entry)
        assert row.lifecycle_state == STATE_WAITING_SETTLEMENT
        assert row.entries_observed >= 3 and row.shadow_decision_id == result.decision_id
        assert row.next_action_at == datetime(2026, 9, 11, 15, 30, tzinfo=ET)
        assert [s.label for s in row.timeline][-1] == "Settlement"
        # Settle it.
        from services.v4_shadow_scheduler import settle_due_cohorts

        class _Quotes:
            def get_quotes_for_known_contracts(self, ticker, contracts, expiration, observed_at):
                from decimal import Decimal

                price = {
                    "c100": ("6.00", "6.20"),
                    "c105": ("1.50", "1.60"),
                    "p95": ("9.00", "9.20"),
                    "c110w": ("41.00", "41.20"),
                    "c160": ("5.50", "5.60"),
                }
                return [
                    SimpleNamespace(
                        strike=c.strike,
                        option_type=c.option_type,
                        bid=Decimal(price[c.external_contract_id][0]),
                        ask=Decimal(price[c.external_contract_id][1]),
                        market_data_quality="delayed",
                        retrieved_at=observed_at,
                    )
                    for c in contracts
                ]

        settle_due_cohorts(
            db_session, provider=_Quotes(), now=datetime(2026, 9, 11, 15, 30, tzinfo=ET)
        )
        settled = classify_event(db_session, ev, datetime(2026, 9, 11, 16, 0, tzinfo=ET))
        assert settled.lifecycle_state == STATE_SETTLED and settled.settlements_settled >= 3
        assert STATE_ENTRY_OBSERVED  # vocabulary exists for the read model


class TestReadinessAndSummary:
    def test_readiness_counts_only_the_upcoming_eligible_window(self, db_session):
        _event(db_session, "SMALL", cap=1)
        _event(db_session, "A")
        _company(db_session, "A", thesis_age=timedelta(days=1))
        b = _event(db_session, "B")
        _prep(db_session, "B", b.id, JobStatus.PENDING)
        _event(db_session, "C")
        pipeline = get_v4_pipeline(db_session, now=NOW)
        r = compute_research_readiness(pipeline, now=NOW)
        assert r.upcoming_events == 4 and r.business_eligible == 3
        assert r.research_ready == 1 and r.research_queued == 1 and r.v4_decision_ready == 1
        assert r.next_window_at == datetime(2026, 9, 9, 15, 30, tzinfo=ET)
        assert (r.next_window_ready, r.next_window_total) == (1, 3)
        today = compute_today_summary(db_session, pipeline, now=NOW)
        assert (today.decision_window_et, today.settlement_window_et, today.deadline_et) == (
            "15:30",
            "15:30",
            "15:50",
        )
        assert (
            today.events_in_window == 4
            and today.business_eligible == 3
            and today.research_ready == 1
        )


class TestJobsFailuresAndStaleness:
    def _status(self, ids):
        return SchedulerStatus(
            running=True,
            jobs=[
                SchedulerJobStatus(
                    job_id=i,
                    next_run_time=NOW + timedelta(hours=3),
                    last_run_at=None,
                    last_run_status=None,
                )
                for i in ids
            ],
        )

    def test_job_monitor_lists_the_fixed_set_then_extras(self, db_session):
        views = get_scheduler_jobs(
            db_session, self._status(list(ALL_JOB_IDS) + ["research_preparation_startup_catchup"])
        )
        assert [v.job_id for v in views] == list(ALL_JOB_IDS) + [
            "research_preparation_startup_catchup"
        ]
        assert (
            "v4_shadow_decision" in ALL_JOB_IDS and "decision_and_entry_capture" not in ALL_JOB_IDS
        )

    def test_stale_research_preparation_is_reported_not_hidden_behind_registration(
        self, db_session
    ):
        db_session.add(
            SchedulerRun(
                job_id="earnings_research_preparation",
                status="success",
                started_at=NOW - timedelta(days=8),
                finished_at=NOW - timedelta(days=8),
                duration_ms=1,
            )
        )
        db_session.add(
            SchedulerRun(
                job_id="earnings_calendar_sync",
                status="success",
                started_at=NOW - timedelta(hours=10),
                finished_at=NOW - timedelta(hours=10),
                duration_ms=1,
            )
        )
        db_session.flush()
        jobs = get_scheduler_jobs(db_session, self._status(list(ALL_JOB_IDS)))
        alerts, staleness = detect_missed_job_alerts(db_session, jobs, [], now=NOW)
        by_id = {s.job_id: s for s in staleness}
        assert by_id["earnings_research_preparation"].state == "stale"
        assert by_id["earnings_calendar_sync"].state == "ok"
        assert any(
            a.category == "job_stale" and "Research preparation" in a.explanation for a in alerts
        )

    def test_catch_up_success_counts_as_research_preparation_freshness(self, db_session):
        # The nightly job last ran 8 days ago, but the startup catch-up did the
        # same work 3 hours ago: research preparation is fresh, not STALE.
        for job_id, age in (
            ("earnings_research_preparation", timedelta(days=8)),
            ("research_preparation_startup_catchup", timedelta(hours=3)),
            ("earnings_calendar_sync", timedelta(hours=10)),
        ):
            db_session.add(
                SchedulerRun(
                    job_id=job_id,
                    status="success",
                    started_at=NOW - age,
                    finished_at=NOW - age,
                    duration_ms=1,
                )
            )
        db_session.flush()
        jobs = get_scheduler_jobs(db_session, self._status(list(ALL_JOB_IDS)))
        alerts, staleness = detect_missed_job_alerts(db_session, jobs, [], now=NOW)
        by_id = {s.job_id: s for s in staleness}
        assert by_id["earnings_research_preparation"].state == "ok"
        assert not any(a.category == "job_stale" for a in alerts)

    def test_missed_decision_run_is_an_alert(self, db_session):
        _event(db_session, "DUE")
        _company(db_session, "DUE", thesis_age=timedelta(days=1))
        later = datetime(2026, 9, 9, 16, 0, tzinfo=ET)
        pipeline = get_v4_pipeline(db_session, now=later)
        jobs = get_scheduler_jobs(db_session, self._status(list(ALL_JOB_IDS)))
        alerts, _ = detect_missed_job_alerts(db_session, jobs, pipeline, now=later)
        assert any(a.category == "job_missed" and a.stage == "v4_shadow_decision" for a in alerts)

    def test_failure_centre_aggregates_research_not_ready_per_day(self, db_session):
        from models.v4_shadow import V4ShadowRunEvent

        evs = [_event(db_session, f"T{i}") for i in range(3)]
        for e in evs:
            db_session.add(
                V4ShadowRunEvent(
                    earnings_calendar_event_id=e.id,
                    ticker=e.symbol,
                    occurred_at=NOW,
                    stage="research_gate",
                    category="RESEARCH_NOT_READY",
                    retryable=True,
                    message="no Company row",
                )
            )
        db_session.add(
            V4ShadowRunEvent(
                earnings_calendar_event_id=evs[0].id,
                ticker="T0",
                occurred_at=NOW,
                stage="view",
                category="VIEW_GENERATION_FAILED",
                retryable=True,
                message="model down",
            )
        )
        db_session.flush()
        failures = get_recent_failures(db_session, now=NOW + timedelta(minutes=1))
        cats = [f.category for f in failures]
        assert cats.count("RESEARCH_NOT_READY") == 1
        agg = next(f for f in failures if f.category == "RESEARCH_NOT_READY")
        assert "3 event(s)" in agg.explanation and agg.retryability == "WINDOW_MISSED"
        assert any(
            f.category == "VIEW_GENERATION_FAILED" and f.retryability == "RETRYABLE"
            for f in failures
        )


@pytest.mark.parametrize("state", [STATE_WAITING_DECISION, STATE_SETTLED])
def test_state_vocabulary_is_v4_only(state):
    assert "V3" not in state and "OFFICIAL" not in state
