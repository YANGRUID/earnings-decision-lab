"""Live Operations read models, V4-only (V4-only reset, 2026-09-02).

Every state below is derived from persisted rows only. Non-vacuity: each
test seeds the exact rows that produce the state and asserts the state,
the reason and the next action together.
"""

from datetime import UTC, date, datetime, timedelta
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
    compute_forward_window,
    compute_research_readiness,
    compute_today_summary,
    detect_missed_job_alerts,
    forward_pipeline,
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
        """Deliberately dated far from any real run.

        This assertion depends on the database containing NO v4_shadow_decision
        scheduler_run on the date under test. The module's NOW is 2026-09-09,
        and on 2026-09-09 itself the shared test database legitimately contains
        a run stamped with the real wall clock -- so the alert correctly did
        NOT fire and the test failed on exactly one calendar day. The
        production logic is clock-injected and was never wrong; the test was
        reading the real date through the database.
        """
        window_date = date(2027, 3, 10)
        _event(db_session, "DUE", earnings_date=window_date)
        _company(db_session, "DUE", thesis_age=timedelta(days=1))
        later = datetime(2027, 3, 10, 16, 0, tzinfo=ET)
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


class TestForwardWindow:
    """The 15:30 ET forward window read model (settlement-priority hardening)."""

    def test_previews_due_settlements_and_ready_decisions_for_the_next_window(self, db_session):
        from models.v4_shadow import V4ForwardWindowTelemetry

        # Job-level tests elsewhere commit real telemetry rows; this read model
        # must be judged on this test's own rows only.
        db_session.query(V4ForwardWindowTelemetry).delete()
        # A decision-ready event for today's window and one that is not ready.
        _event(db_session, "CPRT")
        _company(db_session, "CPRT", thesis_age=timedelta(days=1))
        _event(db_session, "SNOW")
        pipeline = get_v4_pipeline(db_session, now=NOW)
        fw = compute_forward_window(db_session, pipeline, now=NOW)
        assert fw.window_time_et == "15:30"
        assert fw.priority == ("Due settlements", "New decision observations")
        assert fw.next_window_at is not None
        assert fw.next_window_at.astimezone(ET).strftime("%H:%M") == "15:30"
        assert fw.decisions_ready == ("CPRT",)
        assert "SNOW" in fw.decisions_not_ready
        assert fw.settlements_due == ()
        assert fw.last_window_started_at is None and fw.last_settlements_due == 0

    def test_last_window_telemetry_is_summarised_from_persisted_rows(self, db_session):
        from models.v4_shadow import V4ForwardWindowTelemetry

        db_session.query(V4ForwardWindowTelemetry).delete()
        started = datetime(2026, 9, 8, 15, 30, 1, tzinfo=ET)
        rows = [
            V4ForwardWindowTelemetry(
                phase="settlement",
                symbol="AVGO",
                shadow_decision_id=None,
                job_started_at=started,
                completed_at=started + timedelta(seconds=9),
                lock_wait_ms=1200,
                total_ms=9000,
                outcome="completed",
                detail="settled=1 failed=0 window_missed=0 not_due=0",
            ),
            V4ForwardWindowTelemetry(
                phase="decision",
                shadow_decision_id=None,
                job_started_at=started,
                completed_at=started + timedelta(seconds=400),
                lock_wait_ms=350,
                total_ms=400_000,
                outcome="completed",
                detail="ranked=4 no_action=1 failed=0 research_not_ready=0 deadline_skipped=1",
            ),
        ]
        db_session.add_all(rows)
        db_session.flush()
        # A settlement attempt row needs a real decision; use the summary rows only
        # for the phase-level figures, which is what Operations reports.
        fw = compute_forward_window(db_session, [], now=NOW)
        assert fw.last_window_started_at == started
        assert fw.last_decisions_ready == 6 and fw.last_deadline_skipped == 1
        assert fw.last_decision_lock_wait_ms == 350
        assert fw.next_window_at is None and fw.settlements_due == ()


class TestForwardPipeline:
    """The product shows the V4 era only: open/future windows plus V4 evidence."""

    def test_past_windows_without_v4_evidence_are_hidden_but_future_and_evidence_rows_stay(
        self, db_session
    ):
        # A window that passed before V4 could act (yesterday, AMC) with no decision.
        _event(db_session, "DELL", earnings_date=date(2026, 9, 8))
        # Tomorrow's window: shown even before research is ready.
        _event(db_session, "CPRT", earnings_date=date(2026, 9, 10))
        # Today's window, still ahead of NOW (noon): shown.
        _event(db_session, "ZS")
        pipeline = get_v4_pipeline(db_session, now=NOW)
        symbols = {p.symbol for p in pipeline}
        assert {"DELL", "CPRT", "ZS"} <= symbols
        shown = {p.symbol for p in forward_pipeline(pipeline, now=NOW)}
        assert "DELL" not in shown
        assert {"CPRT", "ZS"} <= shown
        # A row that carries V4 evidence is always shown, however old.
        dell = next(p for p in pipeline if p.symbol == "DELL")
        from dataclasses import replace

        with_evidence = replace(dell, shadow_decision_id=5, lifecycle_state="SETTLED")
        assert "DELL" in {p.symbol for p in forward_pipeline([with_evidence], now=NOW)}


class TestListingRuleInThePipeline:
    def test_a_us_listed_foreign_company_is_not_business_ineligible(self, db_session):
        _event(db_session, "LULU", country="CA")
        _event(db_session, "SHOP", country="CA")
        lookup = lambda symbol: "Nasdaq" if symbol == "LULU" else None  # noqa: E731
        by_symbol = {p.symbol: p for p in get_v4_pipeline(db_session, now=NOW, us_listing=lookup)}
        assert by_symbol["LULU"].lifecycle_state != "BUSINESS_INELIGIBLE"
        assert by_symbol["SHOP"].lifecycle_state == "BUSINESS_INELIGIBLE"
        assert "no SEC-registered US exchange listing" in (by_symbol["SHOP"].lifecycle_reason or "")
        # Without a lookup the domicile rule stands (deterministic, no network).
        old = {p.symbol: p for p in get_v4_pipeline(db_session, now=NOW)}
        assert old["LULU"].lifecycle_state == "BUSINESS_INELIGIBLE"


class TestWhyAnEventWasNotDecided:
    """2026-09-17 audit: every miss read "RESEARCH NOT READY -- no Company row",
    whatever the real cause, and a decided event older than two days read
    "outside the V4 window" in the calendar."""

    LATER = datetime(2026, 9, 9, 16, 0, tzinfo=ET)

    def _not_ready_event(self, db, symbol, **kwargs):
        from models.v4_shadow import V4ShadowRunEvent

        row = _event(db, symbol, **kwargs)
        db.add(
            V4ShadowRunEvent(
                earnings_calendar_event_id=row.id,
                ticker=symbol,
                occurred_at=self.LATER - timedelta(minutes=25),
                stage="research_gate",
                category="RESEARCH_NOT_READY",
                retryable=True,
                message="no AI thesis has been prepared for this company",
            )
        )
        db.flush()
        return row

    def test_a_truncated_thesis_is_named(self, db_session):
        row = self._not_ready_event(db_session, "TRUNC")
        company = _company(db_session, "TRUNC")
        # Point-in-time: prepared before the window, as GIS was on 2026-09-09.
        company.created_at = NOW - timedelta(days=3)
        db_session.add(
            ResearchPreparationJob(
                ticker="TRUNC",
                earnings_calendar_event_id=row.id,
                created_at=NOW - timedelta(days=3),
                completed_at=NOW - timedelta(days=3),
                status=JobStatus.COMPLETED_WITH_WARNINGS,
                steps=[
                    {
                        "step": "ai_thesis",
                        "status": "failed",
                        "detail": "thesis generation failed: finish_reason=length",
                    }
                ],
                started_at=NOW - timedelta(days=3),
                attempt_count=1,
            )
        )
        db_session.flush()

        result = classify_event(db_session, row, self.LATER)
        assert result.lifecycle_state == STATE_RESEARCH_NOT_READY
        assert result.window_status == "PASSED"
        assert "window missed" in result.lifecycle_reason
        assert "finish_reason=length" in result.lifecycle_reason

    def test_an_event_never_queued_because_its_calendar_row_was_skipped_is_named(self, db_session):
        from models.v4_shadow import V4ShadowRunEvent

        row = _event(db_session, "SKIPD")
        row.status = "SKIPPED"
        row.vanished_by = "finnhub"
        db_session.add(
            V4ShadowRunEvent(
                earnings_calendar_event_id=row.id,
                ticker="SKIPD",
                occurred_at=self.LATER - timedelta(minutes=25),
                stage="research_gate",
                category="RESEARCH_NOT_READY",
                retryable=True,
                message="no Company row exists for this calendar event yet",
            )
        )
        db_session.flush()

        # The pre-fix gate recorded RESEARCH_NOT_READY; today the calendar gate
        # would refuse it outright. The read model names the calendar either way.
        result = classify_event(db_session, row, self.LATER)
        assert result.lifecycle_state == "CALENDAR_UNCORROBORATED"
        assert "finnhub" in result.lifecycle_reason

    def test_an_out_of_scope_company_reads_not_eligible_whatever_its_calendar_status(
        self, db_session
    ):
        row = _event(db_session, "SMALLSK", cap=500_000_000)
        row.status = "SKIPPED"
        db_session.flush()
        assert classify_event(db_session, row, NOW).lifecycle_state == "BUSINESS_INELIGIBLE"

    def test_a_thesis_written_after_the_window_is_not_called_stale(self, db_session):
        """SNOW, 2026-09-02: prepared that evening, after its 15:30 window. The
        cause must describe the window, not today's thesis age."""
        row = self._not_ready_event(db_session, "LATEX")
        company = _company(db_session, "LATEX")
        company.created_at = NOW - timedelta(days=2)
        db_session.add(
            AIThesisVersion(
                company_id=company.id,
                business_context="b",
                historical_earnings_pattern="h",
                guidance_trend="g",
                key_risks="k",
                market_setup="m",
                disclaimer="d",
                citations=[],
                provider="deepseek",
                model="deepseek-v4-flash",
                created_at=self.LATER + timedelta(hours=5),
            )
        )
        db_session.flush()

        reason = classify_event(db_session, row, self.LATER + timedelta(days=9)).lifecycle_reason
        assert "only generated after the window" in reason

    def test_a_verdict_about_another_event_is_not_used(self, db_session):
        """TCOM: its only enqueue verdicts were about an August event."""
        from models.scheduler_run import SchedulerRun, SchedulerRunEvent

        row = self._not_ready_event(db_session, "OTHEREV")
        other = _event(db_session, "OTHEREV", earnings_date=date(2026, 8, 20))
        run = SchedulerRun(job_id="earnings_research_preparation", started_at=NOW, status="success")
        db_session.add(run)
        db_session.flush()
        db_session.add(
            SchedulerRunEvent(
                scheduler_run_id=run.id,
                earnings_calendar_event_id=other.id,
                symbol="OTHEREV",
                stage="preparation",
                outcome="filtered_out",
                reason="not US listed (country=SG)",
                occurred_at=NOW - timedelta(days=20),
            )
        )
        db_session.flush()

        reason = classify_event(db_session, row, self.LATER).lifecycle_reason
        assert "country=SG" not in reason

    def test_a_future_window_is_ahead_not_missed(self, db_session):
        row = _event(db_session, "AHEADX", earnings_date=date(2026, 9, 10))
        result = classify_event(db_session, row, NOW)
        assert result.window_status == "AHEAD"

    def test_a_share_class_duplicate_is_its_own_state(self, db_session):
        _event(db_session, "DUPX")
        klass = _event(db_session, "DUPX.B")
        result = classify_event(db_session, klass, NOW)
        assert result.lifecycle_state == "DUPLICATE_LISTING"
        assert "DUPX" in result.lifecycle_reason

    def test_an_explicit_range_reaches_a_decided_event_older_than_two_days(self, db_session):
        old = _event(db_session, "OLDKR", earnings_date=date(2026, 9, 1))
        default = {p.symbol for p in get_v4_pipeline(db_session, now=NOW)}
        ranged = {
            p.symbol
            for p in get_v4_pipeline(
                db_session, now=NOW, start=date(2026, 9, 1), end=date(2026, 9, 30)
            )
        }
        assert old.symbol not in default
        assert old.symbol in ranged


class TestCalendarProviderUsage:
    """Proven 2026-09-13 .. 09-17: EarningsAPI's monthly allowance was spent,
    the calendar ran on Finnhub alone, and nothing on Operations said so."""

    NOW = datetime(2031, 5, 20, 12, 0, tzinfo=UTC)

    def _usage(self, db, *, at, success, status_code=None, units=None, credential=None):  # noqa: PLR0913
        from models.provider_usage_event import ProviderUsageEvent

        db.add(
            ProviderUsageEvent(
                provider="earningsapi",
                domain="earnings_calendar",
                operation="get_earnings_calendar",
                occurred_at=at,
                success=success,
                latency_ms=10,
                status_code=status_code,
                rate_limited=not success,
                provider_units=units,
                credential_fingerprint=credential,
            )
        )
        db.flush()

    def test_requests_are_counted_per_date_and_a_spent_month_is_named(self, db_session):
        from services.operations import get_calendar_provider_usage

        self._usage(db_session, at=self.NOW - timedelta(hours=3), success=True, units=8)
        self._usage(
            db_session,
            at=self.NOW - timedelta(hours=1),
            success=False,
            status_code="FREE_QUOTA_EXCEEDED",
        )

        usage = get_calendar_provider_usage(db_session, now=self.NOW)
        assert usage.requests_recorded_today == 9
        assert usage.plan_daily_limit == 100
        assert usage.quota_state == "MONTHLY_EXHAUSTED"

    def test_a_later_success_clears_the_exhausted_state(self, db_session):
        from services.operations import get_calendar_provider_usage

        self._usage(
            db_session,
            at=self.NOW - timedelta(hours=5),
            success=False,
            status_code="DAILY_QUOTA_EXCEEDED",
        )
        self._usage(db_session, at=self.NOW - timedelta(hours=1), success=True)

        assert get_calendar_provider_usage(db_session, now=self.NOW).quota_state == "OK"


class TestUsageAfterAKeyRotation:
    """Measured defect (2026-09-17): EarningsAPI's free allowance was spent, a
    new key was installed the same afternoon, and the Operations card read
    "126 / 100 today, 910 / 1000 this month" for a key that had sent about
    forty requests and been refused none. The counts are requests THIS
    APPLICATION sent; they only equal one key's quota usage while the key
    never changes.
    """

    NOW = datetime(2031, 5, 20, 18, 0, tzinfo=UTC)
    OLD_KEY = "aaaaaaaaaaaa"
    NEW_KEY = "bbbbbbbbbbbb"

    def _usage(self, db, *, at, success, status_code=None, units=None, credential=None):  # noqa: PLR0913
        from models.provider_usage_event import ProviderUsageEvent

        db.add(
            ProviderUsageEvent(
                provider="earningsapi",
                domain="earnings_calendar",
                operation="get_earnings_calendar",
                occurred_at=at,
                success=success,
                latency_ms=10,
                status_code=status_code,
                rate_limited=not success,
                provider_units=units,
                credential_fingerprint=credential,
            )
        )
        db.flush()

    def test_a_retired_keys_refusal_does_not_exhaust_the_new_key(self, db_session):
        from services.operations import get_calendar_provider_usage

        self._usage(
            db_session,
            at=self.NOW - timedelta(hours=9),
            success=False,
            status_code="FREE_QUOTA_EXCEEDED",
            credential=self.OLD_KEY,
        )
        self._usage(
            db_session, at=self.NOW - timedelta(minutes=5), success=True, credential=self.NEW_KEY
        )

        usage = get_calendar_provider_usage(
            db_session, now=self.NOW, credential_fingerprint=self.NEW_KEY
        )
        assert usage.quota_state == "OK"
        assert usage.last_refusal_on_active_credential is False

    def test_a_refusal_predating_the_new_key_is_proven_not_to_be_its_own(self, db_session):
        """Rows written before attribution existed carry no fingerprint. A
        refusal that predates the active key's own first recorded request still
        cannot belong to it -- proven from the timestamps, not assumed."""
        from services.operations import get_calendar_provider_usage

        self._usage(
            db_session,
            at=self.NOW - timedelta(hours=9),
            success=False,
            status_code="FREE_QUOTA_EXCEEDED",
            credential=None,
        )
        self._usage(
            db_session, at=self.NOW - timedelta(minutes=5), success=True, credential=self.NEW_KEY
        )

        usage = get_calendar_provider_usage(
            db_session, now=self.NOW, credential_fingerprint=self.NEW_KEY
        )
        assert usage.last_refusal_on_active_credential is False
        assert usage.quota_state == "OK"

    def test_the_active_keys_own_refusal_still_exhausts_it(self, db_session):
        """The attribution must not become a way to ignore a real refusal."""
        from services.operations import get_calendar_provider_usage

        self._usage(
            db_session, at=self.NOW - timedelta(hours=2), success=True, credential=self.NEW_KEY
        )
        self._usage(
            db_session,
            at=self.NOW - timedelta(minutes=5),
            success=False,
            status_code="FREE_QUOTA_EXCEEDED",
            credential=self.NEW_KEY,
        )

        usage = get_calendar_provider_usage(
            db_session, now=self.NOW, credential_fingerprint=self.NEW_KEY
        )
        assert usage.quota_state == "MONTHLY_EXHAUSTED"
        assert usage.last_refusal_on_active_credential is True

    def test_the_counts_stay_whole_app_requests_and_the_key_share_is_separate(self, db_session):
        """Both numbers are published, and neither is presented as the other:
        the app sent 30 requests this month, 10 of them on the key in use."""
        from services.operations import get_calendar_provider_usage

        self._usage(
            db_session,
            at=self.NOW - timedelta(days=2),
            success=True,
            units=20,
            credential=self.OLD_KEY,
        )
        self._usage(
            db_session,
            at=self.NOW - timedelta(hours=1),
            success=True,
            units=10,
            credential=self.NEW_KEY,
        )

        usage = get_calendar_provider_usage(
            db_session, now=self.NOW, credential_fingerprint=self.NEW_KEY
        )
        assert usage.requests_recorded_this_month == 30
        assert usage.requests_on_active_credential == 10
        assert usage.credential_first_seen_at == self.NOW - timedelta(hours=1)

    def test_no_remaining_allowance_is_published(self, db_session):
        """The invented figure. A remaining balance is the provider's own
        accounting against a key whose earlier life this deployment may never
        have observed, so it is not derived from these counts."""
        from services.operations import CalendarProviderUsage

        fields = set(CalendarProviderUsage.__dataclass_fields__)
        assert not {f for f in fields if "remaining" in f}

    def test_nothing_derived_from_a_key_is_published_over_the_api(self, db_session):
        """The fingerprint identifies a key; the UI never needs its value."""
        from schemas.api import CalendarProviderUsageResponse

        assert "credential_fingerprint" not in CalendarProviderUsageResponse.model_fields


class TestNextWindowReadiness:
    """The preflight card. Its one job is to be wrong in the safe direction:
    never green while a dependency the window needs is degraded.
    """

    NOW = datetime(2031, 5, 18, 14, 0, tzinfo=UTC)

    def _health(self, *, ibkr_green=True, calendar_green=True):
        from services.operations import EarningsCalendarHealth, IbkrHealth

        return (
            IbkrHealth(
                state="green" if ibkr_green else "red",
                gateway_reachable=True,
                authenticated=True,
                connected=ibkr_green,
                live_account=None,
                market_data_quality=None,
                last_heartbeat_at=None,
                last_error=None if ibkr_green else "IB Gateway has lost its connection to IBKR",
                provider="tws",
            ),
            EarningsCalendarHealth(
                state="green" if calendar_green else "red",
                active_provider="earningsapi",
                fallback_provider="finnhub",
                last_successful_sync_at=None,
                events_received=10,
                last_error=None,
                next_scheduled_sync_at=None,
            ),
        )

    def _event(self, db, *, symbol, timing, when, cap="50000000000"):
        from decimal import Decimal

        from models.earnings_calendar_event import EarningsCalendarEvent
        from models.enums import EarningsCalendarEventStatus, EarningsSource

        row = EarningsCalendarEvent(
            symbol=symbol,
            company_name=f"{symbol} Inc",
            earnings_date=when,
            earnings_time=timing,
            status=EarningsCalendarEventStatus.UPCOMING,
            source=EarningsSource.EARNINGSAPI,
            last_confirmed_by="earningsapi",
            market_cap=Decimal(cap),
            country="US",
            created_at=datetime(2031, 5, 1, tzinfo=UTC),
        )
        db.add(row)
        db.flush()
        return row

    def _company_with_thesis(self, db, symbol, thesis_at):
        from models.ai_thesis_version import AIThesisVersion
        from models.company import Company

        company = Company(ticker=symbol, name=f"{symbol} Inc")
        db.add(company)
        db.flush()
        db.add(
            AIThesisVersion(
                company_id=company.id,
                business_context="b",
                historical_earnings_pattern="h",
                guidance_trend="g",
                key_risks="k",
                market_setup="m",
                disclaimer="d",
                citations=[],
                provider="deepseek",
                model="deepseek-v4-flash",
                created_at=thesis_at,
            )
        )
        db.flush()
        return company

    def test_a_thesis_fresh_now_but_stale_at_the_window_is_not_ready(self, db_session):
        """The defect this guards. Freshness judged against NOW reports a
        window as ready that will refuse the event as RESEARCH_NOT_READY when
        it opens -- a green board and a lost window at the same time."""
        from datetime import date

        from models.enums import EarningsTiming
        from services.operations import get_next_window_readiness
        from services.research_orchestration import THESIS_FRESHNESS_DAYS

        symbol = "TESTFRESH"
        window_day = date(2031, 5, 30)
        # Written today, so fresh now -- and older than the limit by the window.
        self._company_with_thesis(db_session, symbol, self.NOW)
        assert (window_day - self.NOW.date()).days > THESIS_FRESHNESS_DAYS
        self._event(db_session, symbol=symbol, timing=EarningsTiming.AMC, when=window_day)

        ibkr, calendar = self._health()
        readiness = get_next_window_readiness(
            db_session, now=self.NOW, ibkr=ibkr, earnings_calendar=calendar
        )
        check = next(c for c in readiness.checks if c.name == "Research ready at the window")
        assert check.ok is False
        assert "must be refreshed" in check.detail
        assert readiness.ready is False

    def test_a_thesis_still_fresh_at_the_window_is_ready(self, db_session):
        from datetime import date

        from models.enums import EarningsTiming
        from services.operations import get_next_window_readiness

        symbol = "TESTREADY"
        self._company_with_thesis(db_session, symbol, self.NOW)
        self._event(db_session, symbol=symbol, timing=EarningsTiming.AMC, when=date(2031, 5, 21))

        ibkr, calendar = self._health()
        readiness = get_next_window_readiness(
            db_session, now=self.NOW, ibkr=ibkr, earnings_calendar=calendar
        )
        assert readiness.symbol == symbol
        assert readiness.ready is True
        assert all(c.ok for c in readiness.checks)

    def test_one_degraded_dependency_makes_the_whole_window_not_ready(self, db_session):
        """A window with no market data produces nothing, whatever else is
        green -- so the card must never summarise its way to READY."""
        from datetime import date

        from models.enums import EarningsTiming
        from services.operations import get_next_window_readiness

        symbol = "TESTTWS"
        self._company_with_thesis(db_session, symbol, self.NOW)
        self._event(db_session, symbol=symbol, timing=EarningsTiming.AMC, when=date(2031, 5, 21))

        ibkr, calendar = self._health(ibkr_green=False)
        readiness = get_next_window_readiness(
            db_session, now=self.NOW, ibkr=ibkr, earnings_calendar=calendar
        )
        assert readiness.ready is False
        assert next(c for c in readiness.checks if c.name == "Market data").ok is False

    def test_an_unconfirmed_session_is_reported_as_the_blocker(self, db_session):
        from datetime import date

        from models.enums import EarningsTiming
        from services.operations import get_next_window_readiness

        symbol = "TESTNOSESS"
        self._company_with_thesis(db_session, symbol, self.NOW)
        self._event(
            db_session, symbol=symbol, timing=EarningsTiming.UNKNOWN, when=date(2031, 5, 21)
        )

        ibkr, calendar = self._health()
        readiness = get_next_window_readiness(
            db_session, now=self.NOW, ibkr=ibkr, earnings_calendar=calendar
        )
        assert readiness.ready is False
        timing = next(c for c in readiness.checks if c.name == "Earnings timing confirmed")
        assert timing.ok is False

    def test_an_event_whose_window_has_closed_is_not_the_next_one(self, db_session):
        from datetime import date

        from models.enums import EarningsTiming
        from services.operations import get_next_window_readiness

        self._company_with_thesis(db_session, "TESTPAST", self.NOW)
        self._company_with_thesis(db_session, "TESTNEXT", self.NOW)
        # BMO on the 18th decided at 15:30 ET on the previous trading day,
        # which is long past at this NOW -- so it is not the next window.
        self._event(
            db_session, symbol="TESTPAST", timing=EarningsTiming.BMO, when=date(2031, 5, 18)
        )
        self._event(
            db_session, symbol="TESTNEXT", timing=EarningsTiming.AMC, when=date(2031, 5, 21)
        )

        ibkr, calendar = self._health()
        readiness = get_next_window_readiness(
            db_session, now=self.NOW, ibkr=ibkr, earnings_calendar=calendar
        )
        assert readiness.symbol == "TESTNEXT"

    def test_a_sub_threshold_company_is_never_the_next_window(self, db_session):
        from datetime import date

        from models.enums import EarningsTiming
        from services.operations import get_next_window_readiness

        self._company_with_thesis(db_session, "TESTSMALL", self.NOW)
        self._event(
            db_session,
            symbol="TESTSMALL",
            timing=EarningsTiming.AMC,
            when=date(2031, 5, 19),
            cap="500000000",
        )
        ibkr, calendar = self._health()
        readiness = get_next_window_readiness(
            db_session, now=self.NOW, ibkr=ibkr, earnings_calendar=calendar
        )
        assert readiness is None or readiness.symbol != "TESTSMALL"
