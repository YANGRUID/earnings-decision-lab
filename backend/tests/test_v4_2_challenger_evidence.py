"""V4.2 -- challenger evidence guarantees.

The properties that make a challenger safe to run beside a control: it cannot
rewrite its own record, it cannot write two records for the same window, it
cannot damage the control when it fails, and it cannot quietly multiply
market-data acquisition.
"""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from analytics.decision.v4_2_viability import VIABILITY_GATE_VERSION
from analytics.decision_timing_policy import V4_TIMING_POLICY
from models.earnings_calendar_event import EarningsCalendarEvent
from models.v4_2_challenger import (
    V42ChallengerCandidate,
    V42ChallengerConfigResult,
    V42ChallengerDecision,
)
from models.v4_shadow import (
    SHADOW_SCHEMA_VERSION,
    V4ShadowCandidate,
    V4ShadowCandidateLeg,
    V4ShadowDecision,
)
from services.v4_2_challenger import (
    CHALLENGER_STATUS_ALREADY_FROZEN,
    CHALLENGER_STATUS_FAILED,
    evaluate_and_freeze,
    evaluate_challenger,
    freeze_challenger_decision,
)

D = Decimal
OBSERVED = datetime(2026, 9, 3, 19, 30, tzinfo=UTC)


@pytest.fixture
def control(db_session):
    """A real control decision with a candidate set and observed legs."""
    event = EarningsCalendarEvent(
        symbol="CHLX", company_name="Challenger Co", earnings_date=date(2026, 9, 3),
        earnings_time="AMC", source="EARNINGSAPI", status="UPCOMING",
    )
    db_session.add(event)
    db_session.flush()
    decision = V4ShadowDecision(
        earnings_calendar_event_id=event.id, ticker="CHLX", company_name="Challenger Co",
        legal_decision_window_at=OBSERVED, generated_at=OBSERVED, as_of=OBSERVED,
        status="RANKED", engine_version="v4-test",
        shadow_schema_version=SHADOW_SCHEMA_VERSION,
        decision_timing_policy_version=V4_TIMING_POLICY.version,
        candidate_count=2, rankable_candidate_count=2,
        underlying_price=D("100"), market_data_quality="delayed",
        expected_move={"implied_move_pct": "0.10"},
    )
    db_session.add(decision)
    db_session.flush()

    for cid, strategy, median, best, pos, nopro in (
        ("good:v1", "bull_call_spread", "0.06", "0.30", "0.50", False),
        ("bad:v1", "iron_butterfly", "-0.09", "-0.02", "0.00", True),
    ):
        candidate = V4ShadowCandidate(
            shadow_decision_id=decision.id, candidate_id=cid, strategy=strategy,
            expiration=date(2026, 9, 18), validity_status="RANKABLE",
            semantic_compatibility=D("1.0"), semantic_tier="strong",
            core_median_return=D(median), core_worst_return=D("-0.10"),
            core_best_return=D(best), core_positive_scenario_fraction=D(pos),
            no_profitable_region=nopro, mean_relative_spread=D("0.05"),
            capital_utilisation=D("0.10"), entry_cash_required=D("200"),
        )
        db_session.add(candidate)
        db_session.flush()
        # Two legs, one contract SHARED between the candidates, so the
        # dedup assertion below is meaningful.
        for index, conid in enumerate(("111", "222" if cid.startswith("good") else "111")):
            db_session.add(V4ShadowCandidateLeg(
                shadow_candidate_id=candidate.id, leg_index=index, action="buy",
                right="call", strike=D("100"), quantity=1, multiplier=D("100"),
                external_contract_id=conid, bid=D("1.00"), ask=D("1.10"),
            ))
    db_session.flush()
    return decision


class TestEvaluationIsReadOnly:
    def test_a_dry_run_writes_nothing(self, db_session, control):
        evaluate_and_freeze(db_session, control, dry_run=True)
        db_session.flush()
        assert db_session.query(V42ChallengerDecision).count() == 0

    def test_the_gate_refuses_the_no_profitable_region_candidate(self, db_session, control):
        out = evaluate_challenger(db_session, control)
        refused = [r for r in out.candidate_rows if not r["viability_acceptable"]]
        assert any("NO_PROFITABLE_REGION" in r["viability_reason_codes"] for r in refused)

    def test_the_control_evidence_is_never_modified(self, db_session, control):
        before = [
            (c.candidate_id, c.rank, c.validity_status)
            for c in db_session.query(V4ShadowCandidate).filter_by(shadow_decision_id=control.id)
        ]
        evaluate_and_freeze(db_session, control, dry_run=False)
        db_session.flush()
        after = [
            (c.candidate_id, c.rank, c.validity_status)
            for c in db_session.query(V4ShadowCandidate).filter_by(shadow_decision_id=control.id)
        ]
        assert before == after
        assert control.status == "RANKED"


class TestQuoteReuse:
    def test_the_challenger_issues_no_market_data_requests(self, db_session, control):
        """It reasons over the control's frozen observations, so a parallel
        run costs no extra subscription."""
        out = evaluate_challenger(db_session, control)
        assert out.market_data_requests_issued == 0

    def test_a_contract_shared_across_candidates_is_counted_once(self, db_session, control):
        """Two candidates, three leg rows, two distinct contracts."""
        out = evaluate_challenger(db_session, control)
        assert out.unique_contracts_reused == 2

    def test_six_configurations_do_not_multiply_acquisition(self, db_session, control):
        out = evaluate_challenger(db_session, control)
        assert len(out.config_rows) == 6
        assert out.market_data_requests_issued == 0


class TestPersistence:
    def test_freezing_writes_the_decision_candidates_and_configs(self, db_session, control):
        out = evaluate_and_freeze(db_session, control, dry_run=False)
        db_session.flush()
        assert out.decision_id is not None
        assert db_session.query(V42ChallengerDecision).count() == 1
        assert db_session.query(V42ChallengerCandidate).count() == 2
        assert db_session.query(V42ChallengerConfigResult).count() == 6

    def test_the_complete_candidate_set_is_kept_not_just_the_winner(self, db_session, control):
        evaluate_and_freeze(db_session, control, dry_run=False)
        db_session.flush()
        rows = db_session.query(V42ChallengerCandidate).all()
        assert {r.viability_acceptable for r in rows} == {True, False}

    def test_the_move_context_actually_used_is_frozen(self, db_session, control):
        evaluate_and_freeze(db_session, control, dry_run=False)
        db_session.flush()
        row = db_session.query(V42ChallengerDecision).one()
        assert row.historical_timing_quality is not None
        assert row.move_distribution_version is not None
        assert row.reaction_anchoring_version is not None
        assert row.historical_as_of is not None

    def test_every_versioned_component_is_recorded(self, db_session, control):
        evaluate_and_freeze(db_session, control, dry_run=False)
        db_session.flush()
        row = db_session.query(V42ChallengerDecision).one()
        for field in ("gate_version", "move_edge_version", "expiry_ladder_version",
                      "friction_version", "ranking_version", "schema_version"):
            assert getattr(row, field), f"{field} must be recorded to explain this decision later"


class TestImmutabilityAndIdempotency:
    def test_a_frozen_challenger_decision_can_never_be_updated(self, db_session, control):
        evaluate_and_freeze(db_session, control, dry_run=False)
        db_session.flush()
        row = db_session.query(V42ChallengerDecision).one()
        row.no_action_reason = "an operator edited the record"
        with pytest.raises(Exception):  # noqa: B017 -- DB trigger, driver-specific type
            db_session.flush()

    def test_a_frozen_challenger_candidate_can_never_be_updated(self, db_session, control):
        evaluate_and_freeze(db_session, control, dry_run=False)
        db_session.flush()
        row = db_session.query(V42ChallengerCandidate).first()
        row.rank = 99
        with pytest.raises(Exception):  # noqa: B017
            db_session.flush()

    def test_refreezing_the_same_window_is_a_no_op(self, db_session, control):
        first = evaluate_and_freeze(db_session, control, dry_run=False)
        db_session.flush()
        second = evaluate_and_freeze(db_session, control, dry_run=False)
        db_session.flush()
        assert second.status == CHALLENGER_STATUS_ALREADY_FROZEN
        assert second.decision_id == first.decision_id
        assert db_session.query(V42ChallengerDecision).count() == 1

    def test_the_database_refuses_a_duplicate_even_if_the_check_is_bypassed(
        self, db_session, control
    ):
        evaluate_and_freeze(db_session, control, dry_run=False)
        db_session.flush()
        db_session.add(V42ChallengerDecision(
            earnings_calendar_event_id=control.earnings_calendar_event_id,
            ticker="CHLX", generated_at=OBSERVED, observed_at=OBSERVED,
            schema_version="x", gate_version=VIABILITY_GATE_VERSION, move_edge_version="x",
            status="RANKED", candidates_evaluated=0, candidates_accepted=0,
        ))
        with pytest.raises(IntegrityError):
            db_session.flush()


class TestFailureIsolation:
    def test_a_challenger_fault_does_not_destroy_control_work(self, db_session, control):
        """The control row written before the challenger runs must survive a
        challenger explosion -- a bare rollback would have unwound it."""
        marker = EarningsCalendarEvent(
            symbol="KEEPX", company_name="Keep Me", earnings_date=date(2026, 9, 9),
            earnings_time="AMC", source="EARNINGSAPI", status="UPCOMING",
        )
        db_session.add(marker)
        db_session.flush()

        broken = evaluate_challenger(db_session, control)
        broken.candidate_rows = [{"not_a_real_column": True}]
        result = freeze_challenger_decision(db_session, control, broken)

        assert result.status == CHALLENGER_STATUS_FAILED
        assert db_session.query(EarningsCalendarEvent).filter_by(symbol="KEEPX").count() == 1
        assert db_session.query(V4ShadowDecision).filter_by(id=control.id).count() == 1

    def test_a_failed_freeze_leaves_no_partial_challenger_rows(self, db_session, control):
        broken = evaluate_challenger(db_session, control)
        broken.candidate_rows = [{"not_a_real_column": True}]
        freeze_challenger_decision(db_session, control, broken)
        assert db_session.query(V42ChallengerDecision).count() == 0
        assert db_session.query(V42ChallengerCandidate).count() == 0

    def test_evaluation_failure_is_returned_not_raised(self, db_session, control):
        out = evaluate_and_freeze(db_session, None, dry_run=True)  # type: ignore[arg-type]
        assert out.status == CHALLENGER_STATUS_FAILED


class TestNoActionHasNoPosition:
    def test_a_no_action_decision_records_no_selected_candidate(self, db_session, control):
        # The control's own candidate rows are immutable, so a NO_ACTION
        # scenario is built as its own event rather than by editing them.
        event = EarningsCalendarEvent(
            symbol="NOACT", company_name="No Action Co", earnings_date=date(2026, 9, 3),
            earnings_time="AMC", source="EARNINGSAPI", status="UPCOMING",
        )
        db_session.add(event)
        db_session.flush()
        decision = V4ShadowDecision(
            earnings_calendar_event_id=event.id, ticker="NOACT", company_name="No Action Co",
            legal_decision_window_at=OBSERVED, generated_at=OBSERVED, as_of=OBSERVED,
            status="RANKED", engine_version="v4-test",
            shadow_schema_version=SHADOW_SCHEMA_VERSION,
            decision_timing_policy_version=V4_TIMING_POLICY.version,
            candidate_count=1, rankable_candidate_count=1,
            underlying_price=D("100"), market_data_quality="delayed",
            expected_move={"implied_move_pct": "0.10"},
        )
        db_session.add(decision)
        db_session.flush()
        db_session.add(V4ShadowCandidate(
            shadow_decision_id=decision.id, candidate_id="hopeless:v1",
            strategy="iron_butterfly", expiration=date(2026, 9, 18),
            validity_status="RANKABLE", semantic_compatibility=D("1.0"),
            semantic_tier="strong", core_median_return=D("-0.20"),
            core_worst_return=D("-0.30"), core_best_return=D("-0.05"),
            core_positive_scenario_fraction=D("0"), no_profitable_region=True,
            mean_relative_spread=D("0.05"), capital_utilisation=D("0.10"),
        ))
        db_session.flush()
        out = evaluate_and_freeze(db_session, decision, dry_run=False)
        db_session.flush()
        assert out.decision_id is not None
        row = db_session.query(V42ChallengerDecision).filter_by(ticker="NOACT").one()
        assert row.status == "NO_ACTION"
        assert row.selected_candidate_id is None
        assert row.no_action_reason
        # No entry, no settlement, no P&L anywhere -- a refusal is not a
        # position.
        assert db_session.query(V42ChallengerConfigResult).filter(
            V42ChallengerConfigResult.selected_candidate_id.isnot(None)
        ).count() == 0
