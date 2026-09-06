"""V4.2 -- parallel shadow isolation, control priority and the feature flag.

These are the properties that decide whether the challenger is safe to run in
the same window as production at all. None of them is about whether V4.2 makes
good decisions; all of them are about whether V4.2 can hurt V4.1.
"""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from analytics.decision_timing_policy import V4_TIMING_POLICY
from core.config import Settings, get_settings
from models.earnings_calendar_event import EarningsCalendarEvent
from models.v4_2_challenger import (
    V42ChallengerCandidateObservation,
    V42ChallengerConfigEntry,
    V42ChallengerConfigResult,
    V42ChallengerConfigSettlement,
    V42ChallengerDecision,
)
from models.v4_shadow import (
    SHADOW_SCHEMA_VERSION,
    V4ShadowCandidate,
    V4ShadowCandidateLeg,
    V4ShadowDecision,
)
from services.v4_2_parallel import (
    CHALLENGER_HEALTH_DISABLED,
    CHALLENGER_HEALTH_READY,
    OUTCOME_NO_ACTION,
    SUCCESSFUL_OUTCOMES,
    run_challenger_phase,
)

D = Decimal
WINDOW = datetime(2026, 9, 10, 19, 30, tzinfo=UTC)


def _control(db, symbol, *, median="0.08", no_profit=False, generated_at=WINDOW):
    event = EarningsCalendarEvent(
        symbol=symbol,
        company_name=f"{symbol} Co",
        earnings_date=date(2026, 9, 10),
        earnings_time="AMC",
        source="EARNINGSAPI",
        status="UPCOMING",
    )
    db.add(event)
    db.flush()
    decision = V4ShadowDecision(
        earnings_calendar_event_id=event.id,
        ticker=symbol,
        company_name=f"{symbol} Co",
        legal_decision_window_at=generated_at,
        generated_at=generated_at,
        as_of=generated_at,
        status="RANKED",
        engine_version="v4-test",
        shadow_schema_version=SHADOW_SCHEMA_VERSION,
        decision_timing_policy_version=V4_TIMING_POLICY.version,
        candidate_count=1,
        rankable_candidate_count=1,
        underlying_price=D("100"),
        market_data_quality="delayed",
        expected_move={"implied_move_pct": "0.05"},
    )
    db.add(decision)
    db.flush()
    candidate = V4ShadowCandidate(
        shadow_decision_id=decision.id,
        candidate_id="c:v1",
        strategy="bull_call_spread",
        expiration=date(2026, 9, 25),
        validity_status="RANKABLE",
        semantic_compatibility=D("1.0"),
        semantic_tier="strong",
        core_median_return=D(median),
        core_worst_return=D("-0.10"),
        core_best_return=D("0.30"),
        core_positive_scenario_fraction=D("0.60"),
        no_profitable_region=no_profit,
        mean_relative_spread=D("0.05"),
        capital_utilisation=D("0.10"),
        entry_cash_required=D("200"),
    )
    db.add(candidate)
    db.flush()
    db.add(
        V4ShadowCandidateLeg(
            shadow_candidate_id=candidate.id,
            leg_index=0,
            action="buy",
            right="call",
            strike=D("100"),
            quantity=1,
            multiplier=D("100"),
            external_contract_id="111",
            bid=D("1.00"),
            ask=D("1.20"),
            market_data_quality="delayed",
        )
    )
    db.flush()
    return decision


def _settings(**overrides):
    return get_settings().model_copy(update=overrides)


class TestFeatureFlag:
    def test_the_flag_defaults_to_false(self):
        assert Settings(_env_file=None).v4_2_parallel_enabled is False

    def test_with_the_flag_off_the_phase_writes_nothing(self, db_session):
        _control(db_session, "FLGOF")
        before = db_session.query(V42ChallengerDecision).count()
        summary = run_challenger_phase(
            db_session, _settings(v4_2_parallel_enabled=False), provider=None, now=WINDOW
        )
        db_session.flush()
        assert summary.enabled is False
        assert summary.evaluated == 0
        assert summary.health == CHALLENGER_HEALTH_DISABLED
        assert db_session.query(V42ChallengerDecision).count() == before

    def test_with_the_flag_off_no_challenger_job_is_registered(self):
        """The challenger is a PHASE of the existing window, never a second
        15:30 registration that could race the control for it."""
        import services.scheduler as scheduler_module

        source = scheduler_module.__file__
        with open(source) as handle:
            text = handle.read()
        assert "v4_2_parallel_enabled" not in text, (
            "the parallel flag must not gate a scheduler registration: the "
            "challenger runs inside run_forward_window, after the control"
        )

    def test_with_the_flag_on_the_phase_evaluates_and_freezes(self, db_session):
        _control(db_session, "FLGON")
        summary = run_challenger_phase(
            db_session, _settings(v4_2_parallel_enabled=True), provider=None, now=WINDOW
        )
        assert summary.enabled is True
        assert summary.evaluated == 1
        assert db_session.query(V42ChallengerDecision).count() == 1


class TestControlIsUntouched:
    def test_the_challenger_phase_never_modifies_control_evidence(self, db_session):
        control = _control(db_session, "UNTCH")
        before = [
            (c.candidate_id, c.rank, c.validity_status, c.core_median_return)
            for c in db_session.query(V4ShadowCandidate).filter_by(shadow_decision_id=control.id)
        ]
        status_before = control.status
        run_challenger_phase(
            db_session, _settings(v4_2_parallel_enabled=True), provider=None, now=WINDOW
        )
        after = [
            (c.candidate_id, c.rank, c.validity_status, c.core_median_return)
            for c in db_session.query(V4ShadowCandidate).filter_by(shadow_decision_id=control.id)
        ]
        assert before == after
        assert control.status == status_before

    def test_the_challenger_writes_only_challenger_tables(self, db_session):
        from models.v4_shadow import (  # noqa: PLC0415
            V4ShadowCandidateObservation,
            V4ShadowConfigEntry,
            V4ShadowConfigSettlement,
        )

        _control(db_session, "ONLYC")
        counts = {
            model: db_session.query(model).count()
            for model in (
                V4ShadowCandidateObservation,
                V4ShadowConfigEntry,
                V4ShadowConfigSettlement,
                V4ShadowDecision,
                V4ShadowCandidate,
            )
        }
        run_challenger_phase(
            db_session, _settings(v4_2_parallel_enabled=True), provider=None, now=WINDOW
        )
        db_session.flush()
        for model, before in counts.items():
            assert db_session.query(model).count() == before, f"{model.__name__} was written"


class TestFailureIsolation:
    def test_a_synthetic_challenger_exception_is_contained(self, db_session, monkeypatch):
        """The control's work must survive a challenger that throws."""
        control = _control(db_session, "BOOMX")

        def explode(*args, **kwargs):
            raise RuntimeError("synthetic challenger fault")

        monkeypatch.setattr("services.v4_2_challenger.evaluate_challenger", explode)
        summary = run_challenger_phase(
            db_session, _settings(v4_2_parallel_enabled=True), provider=None, now=WINDOW
        )
        # The phase records the failure instead of raising it.
        assert summary.failed >= 1 or summary.evaluated >= 1
        # The control decision is still readable and unchanged.
        db_session.flush()
        assert db_session.get(V4ShadowDecision, control.id).status == "RANKED"

    def test_a_challenger_fault_never_reaches_the_forward_window(self, db_session, monkeypatch):
        import services.v4_shadow_scheduler as scheduler

        _control(db_session, "WINDW")

        def explode(*args, **kwargs):
            raise RuntimeError("synthetic phase fault")

        monkeypatch.setattr("services.v4_2_parallel.run_challenger_phase", explode)
        monkeypatch.setattr(
            scheduler,
            "settle_due_cohorts",
            lambda *a, **k: scheduler.SettlementRunSummary(),
        )
        monkeypatch.setattr(
            "services.v4_shadow_orchestration.run_shadow_decisions_for_due_events",
            lambda *a, **k: object(),
        )
        # Must return normally: the control's phases already completed.
        summary = scheduler.run_forward_window(
            db_session,
            _settings(v4_2_parallel_enabled=True),
            provider=None,
            now=WINDOW,
            candidate_events=[],
        )
        assert summary.challenger is None
        assert summary.settlement is not None

    def test_a_broken_decision_row_does_not_break_the_handler(self, db_session, monkeypatch):
        """Isolation that depends on well-formed input is not isolation."""
        from services.v4_2_challenger import evaluate_and_freeze

        def explode(*args, **kwargs):
            raise RuntimeError("bad input")

        monkeypatch.setattr("services.v4_2_challenger.evaluate_challenger", explode)
        out = evaluate_and_freeze(db_session, None, dry_run=True)
        assert out.status == "FAILED"
        assert out.ticker == "unknown"


class TestControlPriority:
    def test_the_challenger_phase_runs_after_both_control_phases(self, db_session, monkeypatch):
        import services.v4_shadow_scheduler as scheduler

        order: list[str] = []
        monkeypatch.setattr(
            scheduler,
            "settle_due_cohorts",
            lambda *a, **k: (order.append("control_settlement"), scheduler.SettlementRunSummary())[
                1
            ],
        )
        monkeypatch.setattr(
            "services.v4_shadow_orchestration.run_shadow_decisions_for_due_events",
            lambda *a, **k: (order.append("control_decisions"), object())[1],
        )
        monkeypatch.setattr(
            "services.v4_2_parallel.run_challenger_phase",
            lambda *a, **k: (order.append("challenger"), None)[1],
        )
        scheduler.run_forward_window(
            db_session,
            _settings(v4_2_parallel_enabled=True),
            provider=None,
            now=WINDOW,
            candidate_events=[],
        )
        assert order == ["control_settlement", "control_decisions", "challenger"]

    def test_with_the_flag_off_the_challenger_phase_is_not_reached(self, db_session, monkeypatch):
        import services.v4_shadow_scheduler as scheduler

        called: list[str] = []
        monkeypatch.setattr(
            scheduler, "settle_due_cohorts", lambda *a, **k: scheduler.SettlementRunSummary()
        )
        monkeypatch.setattr(
            "services.v4_shadow_orchestration.run_shadow_decisions_for_due_events",
            lambda *a, **k: object(),
        )
        monkeypatch.setattr(
            "services.v4_2_parallel.run_challenger_phase",
            lambda *a, **k: called.append("challenger"),
        )
        scheduler.run_forward_window(
            db_session,
            _settings(v4_2_parallel_enabled=False),
            provider=None,
            now=WINDOW,
            candidate_events=[],
        )
        assert called == []

    def test_the_control_settlement_never_observes_quotes_while_disabled(
        self, db_session, monkeypatch
    ):
        """With the challenger off, nothing watches the control's quote sweep
        at all -- not even a no-op observer."""
        import services.v4_shadow_scheduler as scheduler

        seen: list = []

        def capture(db, **kwargs):
            seen.append(kwargs.get("on_quotes"))
            return scheduler.SettlementRunSummary()

        monkeypatch.setattr(scheduler, "settle_due_cohorts", capture)
        monkeypatch.setattr(
            "services.v4_shadow_orchestration.run_shadow_decisions_for_due_events",
            lambda *a, **k: object(),
        )
        scheduler.run_forward_window(
            db_session,
            _settings(v4_2_parallel_enabled=False),
            provider=None,
            now=WINDOW,
            candidate_events=[],
        )
        assert seen and all(observer is None for observer in seen)


class TestNoActionIsSuccess:
    def test_no_action_is_a_successful_outcome_not_a_failure(self):
        assert OUTCOME_NO_ACTION in SUCCESSFUL_OUTCOMES

    def test_a_no_action_decision_produces_no_position(self, db_session):
        # A candidate with no profitable region cannot clear the gate.
        _control(db_session, "NOACX", median="-0.05", no_profit=True)
        summary = run_challenger_phase(
            db_session, _settings(v4_2_parallel_enabled=True), provider=None, now=WINDOW
        )
        db_session.flush()
        assert summary.no_action == 1
        assert summary.health == CHALLENGER_HEALTH_READY, "declining is not degraded"
        decision = db_session.query(V42ChallengerDecision).one()
        assert decision.status == "NO_ACTION"
        assert db_session.query(V42ChallengerConfigEntry).count() == 0
        assert db_session.query(V42ChallengerConfigSettlement).count() == 0
        assert db_session.query(V42ChallengerCandidateObservation).count() == 0
        # Every configuration declined, and each recorded a reason.
        configs = db_session.query(V42ChallengerConfigResult).all()
        assert configs and all(c.status == "NO_ACTION" for c in configs)
        assert all(c.no_action_reason for c in configs)


class TestTrackRecordSeparation:
    def test_the_challenger_track_record_reads_no_control_row(self, db_session):
        from services.v4_2_track_record import build_challenger_track_record

        _control(db_session, "SEPRT")
        run_challenger_phase(
            db_session, _settings(v4_2_parallel_enabled=True), provider=None, now=WINDOW
        )
        record = build_challenger_track_record(db_session)
        assert record.methodology == "CHALLENGER"
        assert record.cohort == "v4_2_parallel_shadow"
        assert record.actions.events_observed == 1

    def test_the_primary_unit_is_the_event_not_the_configuration(self, db_session):
        from services.v4_2_track_record import build_challenger_track_record

        _control(db_session, "UNITX")
        run_challenger_phase(
            db_session, _settings(v4_2_parallel_enabled=True), provider=None, now=WINDOW
        )
        record = build_challenger_track_record(db_session)
        assert record.actions.events_observed == 1
        # Six configuration rows exist, and they are NOT counted as six events.
        assert db_session.query(V42ChallengerConfigResult).count() == 6
        assert record.actions.events_observed != 6

    def test_a_tiny_sample_always_carries_a_warning(self, db_session):
        from services.v4_2_track_record import build_challenger_track_record

        _control(db_session, "TINYN")
        run_challenger_phase(
            db_session, _settings(v4_2_parallel_enabled=True), provider=None, now=WINDOW
        )
        record = build_challenger_track_record(db_session)
        assert record.warnings
        assert any("event(s) observed" in w for w in record.warnings)

    def test_an_unmeasured_rate_is_none_not_zero(self, db_session):
        from services.v4_2_track_record import build_challenger_track_record

        record = build_challenger_track_record(db_session)
        assert record.actions.events_observed == 0
        assert record.actions.action_rate is None, "no observations is not a 0% action rate"


class TestNoOrderPath:
    @pytest.mark.parametrize(
        "module",
        [
            "services.v4_2_parallel",
            "services.v4_2_challenger_entry",
            "services.v4_2_challenger_settlement",
            "services.v4_2_challenger_recovery",
            "services.v4_2_multi_expiry",
            "services.v4_2_track_record",
        ],
    )
    def test_no_challenger_module_can_place_an_order(self, module):
        import importlib

        source = importlib.import_module(module).__file__
        with open(source) as handle:
            text = handle.read().lower()
        for forbidden in (
            "place_order",
            "submit_order",
            "reqids",
            "placeorder",
            "/iserver/account",
        ):
            assert forbidden not in text, f"{module} references {forbidden}"


class TestProspectiveOnly:
    """A parallel shadow observes going FORWARD. It never manufactures a
    challenger record for an event whose outcome is already known."""

    def test_a_historical_control_decision_is_left_alone(self, db_session):
        from datetime import timedelta

        _control(db_session, "OLDXX", generated_at=WINDOW - timedelta(days=7))
        db_session.flush()
        summary = run_challenger_phase(
            db_session, _settings(v4_2_parallel_enabled=True), provider=None, now=WINDOW
        )
        db_session.flush()
        assert summary.evaluated == 0
        assert summary.skipped_historical == 1
        assert db_session.query(V42ChallengerDecision).count() == 0

    def test_a_decision_from_this_window_is_evaluated(self, db_session):
        _control(db_session, "NEWXX")
        summary = run_challenger_phase(
            db_session, _settings(v4_2_parallel_enabled=True), provider=None, now=WINDOW
        )
        assert summary.evaluated == 1
        assert summary.skipped_historical == 0

    def test_a_mixed_batch_evaluates_only_the_new_one(self, db_session):
        from datetime import timedelta

        _control(db_session, "MIXOL", generated_at=WINDOW - timedelta(days=3))
        _control(db_session, "MIXNW")
        db_session.flush()
        summary = run_challenger_phase(
            db_session, _settings(v4_2_parallel_enabled=True), provider=None, now=WINDOW
        )
        db_session.flush()
        assert summary.evaluated == 1
        assert summary.skipped_historical == 1
        frozen = db_session.query(V42ChallengerDecision).all()
        assert [d.ticker for d in frozen] == ["MIXNW"]
