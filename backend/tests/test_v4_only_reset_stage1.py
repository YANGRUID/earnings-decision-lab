"""V4-only reset, stage 1 (2026-09-02): the 15:30 ET T+1 settlement policy
(v2), the honest AVGO transition, the decision deadline guard, the
research-preparation repair (misfire grace, catch-up passes, AI-thesis
step, readiness-aware enqueue) and the retired V3 scheduler jobs.

Deterministic: no network, no production database, no TWS.
"""

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from analytics.decision_timing_policy import (
    V4_ACTIVE_TIMING_POLICY,
    V4_TIMING_POLICY,
    V4_TIMING_POLICY_V2,
    get_timing_policy,
)
from models.enums import EarningsTiming

ET = ZoneInfo("America/New_York")


def _et(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=ET)


def _event(earnings_date, timing):
    return SimpleNamespace(earnings_date=earnings_date, earnings_time=timing)


class TestTimingPolicyV2:
    def test_v2_is_active_and_settles_at_1530_on_t_plus_one(self):
        assert V4_ACTIVE_TIMING_POLICY is V4_TIMING_POLICY_V2
        assert V4_TIMING_POLICY_V2.version == "v4-1530-entry-1530-t1-settlement-v2"
        assert (V4_TIMING_POLICY_V2.entry_time.hour, V4_TIMING_POLICY_V2.entry_time.minute) == (
            15,
            30,
        )
        assert (V4_TIMING_POLICY_V2.exit_time.hour, V4_TIMING_POLICY_V2.exit_time.minute) == (
            15,
            30,
        )

    def test_v1_stays_in_the_registry_unchanged(self):
        """Rows frozen under v1 (AVGO, 2026-09-02) keep resolving to v1's
        15:55 exit; the transition is prospective, not a reinterpretation."""
        v1 = get_timing_policy("v4-pre-earnings-1530et-v1")
        assert v1 is V4_TIMING_POLICY
        assert (v1.exit_time.hour, v1.exit_time.minute) == (15, 55)
        assert get_timing_policy(V4_TIMING_POLICY_V2.version) is V4_TIMING_POLICY_V2

    def test_amc_never_settles_same_day(self):
        from services.v4_shadow_scheduler import v4_schedule_for_event

        s = v4_schedule_for_event(_event(date(2026, 9, 2), EarningsTiming.AMC))  # Wed AMC
        assert s.entry_timestamp == _et(2026, 9, 2, 15, 30)
        assert s.exit_timestamp == _et(2026, 9, 3, 15, 30)  # D+1, not D0

    def test_bmo_settles_on_the_earnings_day(self):
        from services.v4_shadow_scheduler import v4_schedule_for_event

        s = v4_schedule_for_event(_event(date(2026, 9, 3), EarningsTiming.BMO))  # Thu BMO
        assert s.entry_timestamp == _et(2026, 9, 2, 15, 30)  # D-1
        assert s.exit_timestamp == _et(2026, 9, 3, 15, 30)  # D0


class TestSettlementWindowV2:
    def test_window_is_1530_plus_minus_five_minutes(self):
        from services.v4_shadow_scheduler import (
            SETTLEMENT_DUE,
            SETTLEMENT_NOT_DUE,
            SETTLEMENT_WINDOW_MISSED,
            v4_settlement_window_state,
        )

        avgo = _event(date(2026, 9, 2), EarningsTiming.AMC)
        assert v4_settlement_window_state(avgo, _et(2026, 9, 2, 15, 30)) == SETTLEMENT_NOT_DUE
        assert v4_settlement_window_state(avgo, _et(2026, 9, 2, 15, 55)) == SETTLEMENT_NOT_DUE
        assert v4_settlement_window_state(avgo, _et(2026, 9, 3, 15, 24)) == SETTLEMENT_NOT_DUE
        assert v4_settlement_window_state(avgo, _et(2026, 9, 3, 15, 25)) == SETTLEMENT_DUE
        assert v4_settlement_window_state(avgo, _et(2026, 9, 3, 15, 30)) == SETTLEMENT_DUE
        assert v4_settlement_window_state(avgo, _et(2026, 9, 3, 15, 35)) == SETTLEMENT_DUE
        assert v4_settlement_window_state(avgo, _et(2026, 9, 3, 15, 36)) == SETTLEMENT_WINDOW_MISSED
        assert v4_settlement_window_state(avgo, _et(2026, 9, 3, 15, 55)) == SETTLEMENT_WINDOW_MISSED

    def test_v1_entry_settles_under_v2_and_records_v2_on_the_settlement_only(
        self, db_session, monkeypatch
    ):
        """The AVGO transition: entry rows frozen under v1 are untouched; the
        settlement rows written by the v2 job carry v2."""
        import test_v4_six_cohort_evidence as cohort

        from models.earnings_calendar_event import EarningsCalendarEvent
        from models.v4_shadow import V4ShadowConfigEntry, V4ShadowConfigSettlement, V4ShadowDecision
        from services.v4_shadow_scheduler import settle_due_cohorts

        event = EarningsCalendarEvent(
            symbol="SIXC",
            company_name="Six Cohort Co",
            earnings_date=date(2026, 9, 10),
            earnings_time="AMC",
            source="EARNINGSAPI",
            status="UPCOMING",
        )
        db_session.add(event)
        db_session.flush()
        result = cohort._freeze(db_session, event, monkeypatch=monkeypatch)
        # Whatever version the freeze stamped is immutable; settlement never rewrites it.
        entries = (
            db_session.query(V4ShadowConfigEntry)
            .filter_by(shadow_decision_id=result.decision_id)
            .all()
        )
        frozen_entry_versions = {e.timing_policy_version for e in entries}
        frozen_decision_version = db_session.get(
            V4ShadowDecision, result.decision_id
        ).decision_timing_policy_version

        class _Quotes:
            calls = 0

            def get_quotes_for_known_contracts(self, ticker, contracts, expiration, observed_at):
                type(self).calls += 1
                price = {
                    "c100": ("6.00", "6.20"),
                    "c105": ("1.50", "1.60"),
                    "p95": ("9.00", "9.20"),
                    "c110w": ("41.00", "41.20"),
                    "c160": ("5.50", "5.60"),
                }
                from decimal import Decimal

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

        # Not due at the OLD 15:55 same-day time, due at 15:30 on T+1 (Sep 11).
        s1 = settle_due_cohorts(db_session, provider=_Quotes(), now=_et(2026, 9, 10, 15, 55))
        assert (s1.not_due, s1.settled) == (1, 0)
        s2 = settle_due_cohorts(db_session, provider=_Quotes(), now=_et(2026, 9, 11, 15, 30))
        assert s2.settled == len(entries) and s2.failed == 0
        rows = (
            db_session.query(V4ShadowConfigSettlement)
            .filter_by(shadow_decision_id=result.decision_id)
            .all()
        )
        assert {r.timing_policy_version for r in rows} == {V4_TIMING_POLICY_V2.version}
        # The frozen entry/decision rows still say what they said.
        assert {e.timing_policy_version for e in entries} == frozen_entry_versions
        assert (
            db_session.get(V4ShadowDecision, result.decision_id).decision_timing_policy_version
            == frozen_decision_version
        )


class TestDeadlineGuard:
    def test_deadline_is_1550_eastern(self):
        from analytics.forward_windows import DECISION_DEADLINE_ET, decision_deadline_for

        assert (DECISION_DEADLINE_ET.hour, DECISION_DEADLINE_ET.minute) == (15, 50)
        assert decision_deadline_for(datetime(2026, 9, 3, 19, 31, tzinfo=UTC)) == _et(
            2026, 9, 3, 15, 50
        )

    def test_ready_events_past_the_deadline_are_skipped_and_recorded(self, db_session):
        """Cheap gates still run (RESEARCH_NOT_READY is recorded), but no full
        evaluation starts once the clock passes the deadline."""
        from models.ai_thesis_version import AIThesisVersion
        from models.company import Company
        from models.earnings_calendar_event import EarningsCalendarEvent
        from models.v4_shadow import V4ShadowRunEvent
        from services.v4_shadow_orchestration import run_shadow_decisions_for_due_events

        ready_co = Company(ticker="RDY", name="Ready Co")
        db_session.add(ready_co)
        db_session.flush()
        db_session.add(
            AIThesisVersion(
                company_id=ready_co.id,
                business_context="b",
                historical_earnings_pattern="h",
                guidance_trend="g",
                key_risks="k",
                market_setup="m",
                disclaimer="d",
                citations=[],
                provider="deepseek",
                model="deepseek-v4-flash",
            )
        )
        events = []
        for sym in ("RDY", "NOCO"):
            ev = EarningsCalendarEvent(
                symbol=sym,
                company_name=sym,
                earnings_date=date(2026, 9, 10),
                earnings_time="AMC",
                source="EARNINGSAPI",
                status="UPCOMING",
            )
            db_session.add(ev)
            events.append(ev)
        db_session.flush()

        calls = []

        def view_generator(db, company, event, now):
            calls.append(company.ticker)
            raise AssertionError("must not be called past the deadline")

        deadline = _et(2026, 9, 10, 15, 50)
        summary = run_shadow_decisions_for_due_events(
            db_session,
            None,
            now=_et(2026, 9, 10, 15, 30),
            provider=None,
            view_generator=view_generator,
            due_predicate=lambda e, now: True,
            candidate_events=events,
            deadline=deadline,
            clock=lambda: _et(2026, 9, 10, 15, 51),
        )
        assert calls == []
        assert summary.deadline_skipped == 1 and summary.research_not_ready == 1
        assert {o.status for o in summary.outcomes} == {"DEADLINE_SKIPPED", "RESEARCH_NOT_READY"}
        cats = {e.ticker: e.category for e in db_session.query(V4ShadowRunEvent).all()}
        assert cats["RDY"] == "DEADLINE_SKIPPED" and cats["NOCO"] == "RESEARCH_NOT_READY"

    def test_before_the_deadline_evaluation_proceeds(self, db_session):
        from models.ai_thesis_version import AIThesisVersion
        from models.company import Company
        from models.earnings_calendar_event import EarningsCalendarEvent
        from services.v4_shadow_orchestration import run_shadow_decisions_for_due_events

        co = Company(ticker="RDY2", name="Ready Co")
        db_session.add(co)
        db_session.flush()
        db_session.add(
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
            )
        )
        ev = EarningsCalendarEvent(
            symbol="RDY2",
            company_name="RDY2",
            earnings_date=date(2026, 9, 10),
            earnings_time="AMC",
            source="EARNINGSAPI",
            status="UPCOMING",
        )
        db_session.add(ev)
        db_session.flush()
        seen = []
        summary = run_shadow_decisions_for_due_events(
            db_session,
            None,
            now=_et(2026, 9, 10, 15, 30),
            provider=None,
            view_generator=lambda db, c, e, n: seen.append(c.ticker) or None,
            due_predicate=lambda e, now: True,
            candidate_events=[ev],
            deadline=_et(2026, 9, 10, 15, 50),
            clock=lambda: _et(2026, 9, 10, 15, 49),
        )
        assert seen == ["RDY2"] and summary.deadline_skipped == 0


class TestSchedulerRegistration:
    def _jobs(self, enabled=True):
        from core.config import Settings, get_settings
        from services.scheduler import build_scheduler

        base = get_settings().model_dump()
        base["v4_shadow_enabled"] = enabled
        with patch("services.scheduler.get_settings", return_value=Settings(**base)):
            return {job.id: job for job in build_scheduler().get_jobs()}

    def test_v3_jobs_are_gone_and_v4_settlement_fires_at_1530(self):
        from services.scheduler import (
            RESEARCH_PREPARATION_STARTUP_CATCHUP_JOB_ID,
            RESEARCH_READINESS_CATCHUP_JOB_ID,
            V4_SHADOW_DECISION_JOB_ID,
            V4_SHADOW_SETTLEMENT_JOB_ID,
        )

        jobs = self._jobs(enabled=True)
        assert "decision_and_entry_capture" not in jobs and "exit_capture" not in jobs

        def hhmm(job_id):
            f = jobs[job_id].trigger.fields
            return next(str(x) for x in f if x.name == "hour"), next(
                str(x) for x in f if x.name == "minute"
            )

        assert hhmm(V4_SHADOW_DECISION_JOB_ID) == ("15", "30")
        assert hhmm(V4_SHADOW_SETTLEMENT_JOB_ID) == ("15", "30")
        assert hhmm(RESEARCH_READINESS_CATCHUP_JOB_ID) == ("13", "0")
        assert RESEARCH_PREPARATION_STARTUP_CATCHUP_JOB_ID in jobs

    def test_nightly_jobs_survive_a_restart_spanning_their_minute(self):
        """The Aug 25 -> Sep 1 failure: a 1-second misfire grace dropped every
        run the process was not alive for. Now a run delayed by hours still
        happens once, and the same-day pass exists as a further backstop."""
        from services.scheduler import (
            CALENDAR_SYNC_JOB_ID,
            EARNINGS_RESEARCH_PREPARATION_JOB_ID,
            RESEARCH_READINESS_CATCHUP_JOB_ID,
        )

        jobs = self._jobs()
        assert jobs[EARNINGS_RESEARCH_PREPARATION_JOB_ID].misfire_grace_time >= 3 * 3600
        assert jobs[CALENDAR_SYNC_JOB_ID].misfire_grace_time >= 3 * 3600
        assert jobs[RESEARCH_READINESS_CATCHUP_JOB_ID].misfire_grace_time >= 3600
        assert jobs[EARNINGS_RESEARCH_PREPARATION_JOB_ID].coalesce is True


class TestResearchReadiness:
    def test_prepared_company_without_thesis_is_queued_again(self, db_session):
        from models.company import Company
        from models.earnings_calendar_event import EarningsCalendarEvent
        from models.research_preparation_job import JobStatus, ResearchPreparationJob
        from services.earnings_research_preparation import (
            enqueue_readiness_catchup,
            v4_research_ready,
        )

        now = datetime(2026, 9, 2, 22, 0, tzinfo=UTC)
        db_session.add(Company(ticker="SNOW", name="Snowflake"))
        ev = EarningsCalendarEvent(
            symbol="SNOW",
            company_name="Snowflake",
            earnings_date=date(2026, 9, 3),
            earnings_time="AMC",
            source="EARNINGSAPI",
            status="UPCOMING",
            market_cap=111_875_549_560,
            country="US",
        )
        db_session.add(ev)
        db_session.flush()
        db_session.add(
            ResearchPreparationJob(
                ticker="SNOW",
                earnings_calendar_event_id=ev.id,
                status=JobStatus.COMPLETED,
                steps=[],
                started_at=now - timedelta(hours=6),
                completed_at=now - timedelta(hours=5),
                attempt_count=0,
            )
        )
        db_session.flush()
        assert v4_research_ready(db_session, "SNOW", now=now) == (False, "no AI thesis")

        class _Chain:
            def list_available_expirations(self, symbol, after):
                return [date(2026, 9, 5), date(2026, 9, 12)]

        results = enqueue_readiness_catchup(db_session, _Chain(), now=now, lookahead_days=3)
        snow = next(r for r in results if r.symbol == "SNOW")
        assert snow.outcome == "queued" and "not V4-ready" in (snow.reason or "")
        pending = (
            db_session.query(ResearchPreparationJob)
            .filter_by(ticker="SNOW", status=JobStatus.PENDING)
            .count()
        )
        assert pending == 1

    def test_thesis_step_generates_and_persists_when_stale_and_skips_when_fresh(self, db_session):
        from datetime import UTC as _UTC

        from models.ai_thesis_version import AIThesisVersion
        from models.company import Company
        from models.research_preparation_job import StepStatus
        from services import research_orchestration as ro

        co = Company(ticker="THX", name="Thesis Co")
        db_session.add(co)
        db_session.flush()

        class _Llm:
            name = "deepseek"

        fake_result = SimpleNamespace(
            thesis=SimpleNamespace(
                business_context="b",
                historical_earnings_pattern="h",
                guidance_trend="g",
                key_risks="k",
                market_setup="m",
                disclaimer="d",
            ),
            citations=[],
            generated_at=datetime.now(_UTC),
            model="deepseek-v4-flash",
            estimate_snapshot_id=None,
            volatility_snapshot_id=None,
        )
        generated = []

        def fake_generate(db, llm, embedder, company):
            generated.append(company.ticker)
            return fake_result

        providers = SimpleNamespace(llm=_Llm(), embedder=object())
        with (
            patch("services.earnings_thesis.generate_earnings_thesis", fake_generate),
            patch("services.research_history._citations_to_json", lambda c: []),
        ):
            status, detail = ro._prepare_ai_thesis(
                db_session, providers, co, datetime.now(_UTC), False
            )
            assert status == StepStatus.DONE and "generated" in (detail or "")
            assert generated == ["THX"]
            assert db_session.query(AIThesisVersion).filter_by(company_id=co.id).count() == 1
            # Fresh now -> not regenerated.
            status2, detail2 = ro._prepare_ai_thesis(
                db_session, providers, co, datetime.now(_UTC), False
            )
            assert status2 == StepStatus.DONE and "fresh thesis" in (detail2 or "")
            assert generated == ["THX"]
            # No LLM configured -> honest skip, never a failure.
            status3, _ = ro._prepare_ai_thesis(
                db_session,
                SimpleNamespace(llm=None, embedder=None),
                Company(ticker="X", name="x"),
                datetime.now(_UTC),
                True,
            )
            assert status3 == StepStatus.SKIPPED

    def test_ai_thesis_is_the_last_preparation_step(self):
        from models.research_preparation_job import PreparationStep

        assert list(PreparationStep)[-1] is PreparationStep.AI_THESIS
