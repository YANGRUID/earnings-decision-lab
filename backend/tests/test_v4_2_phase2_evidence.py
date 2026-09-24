"""V4.2 Phase 2 -- what gets frozen, and what the two phases owe each other.

Phase 2 shares Phase 1's tables. That is the right call -- two sets of tables
would mean two entry paths and two settlement paths, and the first fix to one
would leave the other behind -- but it puts a real obligation on both sides:
neither phase may mistake the other's row for its own, and Phase 1's evidence
must come out byte-identical whether or not Phase 2 ran.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from analytics.decision.v4_2_config_policy import (
    ConfigurationDecision,
    ConfigurationDiagnostics,
    SharedCandidate,
)
from analytics.decision.v4_2_phase2_methodology import PHASE_2_METHODOLOGY
from analytics.decision.v4_2_viability import CandidateEconomics
from analytics.decision.v4_configurations import V4_CONFIGURATIONS, size_configuration_position
from analytics.decision_timing_policy import V4_TIMING_POLICY
from models.earnings_calendar_event import EarningsCalendarEvent
from models.v4_2_challenger import (
    V42ChallengerCandidate,
    V42ChallengerConfigResult,
    V42ChallengerDecision,
)
from models.v4_shadow import SHADOW_SCHEMA_VERSION, V4ShadowDecision
from services.v4_2_phase2 import Phase2Evaluation, Phase2Stage
from services.v4_2_phase2_evidence import (
    FREEZE_ALREADY_FROZEN,
    FREEZE_FROZEN,
    freeze_phase2_decision,
    phase2_activated,
)

D = Decimal
OBSERVED = datetime(2026, 9, 24, 19, 30, tzinfo=UTC)
EXPIRY_NEAR = date(2026, 9, 25)
EXPIRY_FAR = date(2026, 10, 16)


@pytest.fixture
def control(db_session):
    event = EarningsCalendarEvent(
        symbol="PHTWO", company_name="Phase Two Co", earnings_date=date(2026, 9, 24),
        earnings_time="AMC", source="EARNINGSAPI", status="UPCOMING",
    )
    db_session.add(event)
    db_session.flush()
    decision = V4ShadowDecision(
        earnings_calendar_event_id=event.id, ticker="PHTWO", company_name="Phase Two Co",
        legal_decision_window_at=OBSERVED, generated_at=OBSERVED, as_of=OBSERVED,
        status="RANKED", engine_version="v4-test",
        shadow_schema_version=SHADOW_SCHEMA_VERSION,
        decision_timing_policy_version=V4_TIMING_POLICY.version,
        candidate_count=0, rankable_candidate_count=0,
        underlying_price=D("100"), market_data_quality="delayed",
        expected_move={"implied_move_pct": "0.10"},
        view_direction="neutral",
    )
    db_session.add(decision)
    db_session.flush()
    return decision


def _shared(candidate_id, strategy, *, median, entry_cash, max_risk, expiration):
    return SharedCandidate(
        candidate_id=candidate_id,
        strategy=strategy,
        economics=CandidateEconomics(
            candidate_id=candidate_id, strategy=strategy, median_return=D(median),
            worst_return=D("-0.08"), best_return=D("0.40"),
            positive_scenario_fraction=D("0.60"), no_profitable_region=False,
            semantic_compatibility=D("0.9"), mean_relative_spread=D("0.05"),
        ),
        entry_cash_required=D(entry_cash),
        per_contract_max_risk=D(max_risk),
        n_legs=2,
        n_legs_with_two_sided_quote=2,
    )


def _evaluation() -> Phase2Evaluation:
    """Two structures on two different expiries, and six configurations that
    split between them -- the shape Phase 1 could never produce."""
    cheap = _shared("bear_put_spread:NARROW@2026-09-25", "bear_put_spread",
                    median="0.10", entry_cash="250", max_risk="250", expiration=EXPIRY_NEAR)
    rich = _shared("bull_call_spread:WIDE@2026-10-16", "bull_call_spread",
                   median="0.28", entry_cash="2600", max_risk="2600", expiration=EXPIRY_FAR)
    evaluation = Phase2Evaluation(ticker="PHTWO", status="ACTION", universe=[cheap, rich])
    evaluation.telemetry.stages = [
        Phase2Stage("underlying", 1, 0, D("12")),
        Phase2Stage("metadata", 1, 0, D("40")),
        Phase2Stage("chain_discovery", 2, 44, D("300")),
        Phase2Stage("quotes", 2, 18, D("500")),
    ]
    evaluation.telemetry.contracts_deduplicated = 9
    evaluation.telemetry.total_latency_ms = D("900")
    evaluation.implied_move_pct = D("0.062")
    evaluation.underlying_price = D("100")
    evaluation.market_data_quality = "delayed"

    for candidate, expiration, ladder in ((cheap, EXPIRY_NEAR, 0), (rich, EXPIRY_FAR, 2)):
        evaluation.candidate_detail[candidate.candidate_id] = {
            "candidate_id": candidate.candidate_id,
            "strategy": candidate.strategy,
            "expiration": expiration,
            "geometry_variant_id": "TEST",
            "validity_status": "RANKABLE",
            "validity_reason": "fully valued",
            "semantic_compatibility": D("0.9"),
            "semantic_tier": "strong",
            "worst_relative_spread": D("0.07"),
            "market_data_quality": "delayed",
            "expiry_ladder_position": ladder,
            "expiry_context": {
                "entry_dte": (expiration - date(2026, 9, 24)).days,
                "dte_at_settlement": (expiration - date(2026, 9, 25)).days,
                "settlement_risk": "expires_on_settlement_day" if ladder == 0 else "clear",
                "implied_move_pct": "0.062" if ladder == 0 else "0.041",
                "implied_move_source": "atm_straddle",
            },
            "legs": [
                {"leg_index": 0, "action": "buy", "right": "put", "strike": "100",
                 "quantity": 1, "multiplier": "100", "required_side": "ask",
                 "bid": "2", "ask": "2.2", "implied_volatility": "0.45",
                 "delta": "-0.5", "bid_size": 10, "ask_size": 10, "volume": 100,
                 "open_interest": 500, "market_data_quality": "delayed",
                 "external_contract_id": f"{ladder}-A",
                 "expiration": expiration.isoformat()},
                {"leg_index": 1, "action": "sell", "right": "put", "strike": "95",
                 "quantity": 1, "multiplier": "100", "required_side": "bid",
                 "bid": "1", "ask": "1.1", "implied_volatility": "0.47",
                 "delta": "-0.3", "bid_size": 8, "ask_size": 8, "volume": 80,
                 "open_interest": 300, "market_data_quality": "delayed",
                 "external_contract_id": f"{ladder}-B",
                 "expiration": expiration.isoformat()},
            ],
        }

    for configuration in V4_CONFIGURATIONS:
        winner = rich if configuration.capital_base >= D("10000") else cheap
        position = size_configuration_position(
            configuration,
            candidate_id=winner.candidate_id,
            per_contract_entry_cash=winner.entry_cash_required,
            per_contract_max_risk=winner.per_contract_max_risk,
        )
        evaluation.configurations.append(
            ConfigurationDecision(
                configuration=configuration,
                status="ACTION",
                selected_candidate_id=winner.candidate_id,
                rank=1,
                position=position,
                diagnostics=ConfigurationDiagnostics(universe_count=2, rankable_count=1,
                                                     capital_rejected_count=1),
                ranked_candidate_ids=(winner.candidate_id,),
                selection_explanation=f"{configuration.label} took {winner.strategy}",
            )
        )
    return evaluation


class TestTheSixRowsCarryTheirOwnSelection:
    def test_configurations_may_persist_different_candidates(self, db_session, control):
        result = freeze_phase2_decision(db_session, control, _evaluation())
        db_session.flush()

        assert result.status == FREEZE_FROZEN
        rows = (
            db_session.query(V42ChallengerConfigResult)
            .filter_by(challenger_decision_id=result.decision_id)
            .all()
        )
        assert len(rows) == 6
        assert len({r.selected_candidate_id for r in rows}) == 2, (
            "the six configuration rows collapsed onto one structure"
        )

    def test_each_row_carries_its_own_size_and_census(self, db_session, control):
        result = freeze_phase2_decision(db_session, control, _evaluation())
        db_session.flush()

        rows = {
            r.configuration_key: r
            for r in db_session.query(V42ChallengerConfigResult)
            .filter_by(challenger_decision_id=result.decision_id)
            .all()
        }
        two_k = rows["v4_2k_conservative"]
        ten_k = rows["v4_10k_aggressive"]
        assert two_k.quantity and ten_k.quantity
        assert two_k.capital_used is not None and ten_k.capital_used is not None
        assert two_k.max_risk_used <= two_k.max_risk_dollars
        assert ten_k.max_risk_used <= ten_k.max_risk_dollars
        assert two_k.rejection_summary["universe_count"] == 2
        assert two_k.ranking_version
        assert two_k.selection_explanation

    def test_the_event_row_has_no_single_winner(self, db_session, control):
        """A Phase-2 event has six decisions, not one. Leaving the
        event-level selection NULL is the honest representation."""
        result = freeze_phase2_decision(db_session, control, _evaluation())
        db_session.flush()

        row = db_session.get(V42ChallengerDecision, result.decision_id)
        assert row.selected_candidate_id is None
        assert row.configurations_actioned == 6
        assert row.distinct_selected_candidates == 2
        assert row.methodology_version == PHASE_2_METHODOLOGY


class TestTheUniverseIsFrozenWhole:
    def test_every_candidate_is_persisted_with_its_own_expiry(self, db_session, control):
        result = freeze_phase2_decision(
            db_session, control, _evaluation(), settlement_date=date(2026, 9, 25)
        )
        db_session.flush()

        rows = (
            db_session.query(V42ChallengerCandidate)
            .filter_by(challenger_decision_id=result.decision_id)
            .all()
        )
        assert len(rows) == 2
        assert {r.expiration for r in rows} == {EXPIRY_NEAR, EXPIRY_FAR}
        assert {r.expiry_ladder_position for r in rows} == {0, 2}
        # Each expiry keeps its own implied move -- never the nearest one's.
        assert len({r.expiry_implied_move_pct for r in rows}) == 2
        assert all(r.legs_json and r.legs_json["legs"] for r in rows)
        assert all(r.validity_status == "RANKABLE" for r in rows)
        assert {r.dte_at_settlement for r in rows} == {0, 21}

    def test_the_request_budget_is_recorded_by_stage(self, db_session, control):
        result = freeze_phase2_decision(db_session, control, _evaluation())
        db_session.flush()

        row = db_session.get(V42ChallengerDecision, result.decision_id)
        stages = {s["stage"] for s in row.request_budget["stages"]}
        assert stages == {"underlying", "metadata", "chain_discovery", "quotes"}
        assert row.market_data_request_count == 6
        assert row.unique_contracts_quoted == 18


class TestThePhasesDoNotCollide:
    def test_a_second_freeze_for_the_same_window_is_a_no_op(self, db_session, control):
        first = freeze_phase2_decision(db_session, control, _evaluation())
        db_session.flush()
        second = freeze_phase2_decision(db_session, control, _evaluation())
        db_session.flush()

        assert second.status == FREEZE_ALREADY_FROZEN
        assert second.decision_id == first.decision_id
        assert (
            db_session.query(V42ChallengerDecision)
            .filter_by(earnings_calendar_event_id=control.earnings_calendar_event_id)
            .count()
            == 1
        )

    def test_phase_1_still_writes_its_own_row_after_phase_2_has_written_one(
        self, db_session, control
    ):
        """The regression this guards: both phases share the table and the
        gate version, so an unfiltered lookup would make Phase 1 believe its
        own decision was already frozen."""
        from services.v4_2_challenger import evaluate_and_freeze  # noqa: PLC0415

        freeze_phase2_decision(db_session, control, _evaluation())
        db_session.flush()

        out = evaluate_and_freeze(db_session, control, dry_run=False)
        db_session.flush()

        assert out.decision_id is not None
        rows = (
            db_session.query(V42ChallengerDecision)
            .filter_by(earnings_calendar_event_id=control.earnings_calendar_event_id)
            .all()
        )
        assert len(rows) == 2
        assert {r.methodology_version for r in rows} == {None, PHASE_2_METHODOLOGY}


class TestActivationIsProspectiveOnly:
    def test_the_flag_alone_does_not_activate(self):
        allowed, why = phase2_activated(
            enabled=True, activation_at=None, legal_decision_window_at=OBSERVED
        )
        assert not allowed
        assert "no activation instant" in why

    def test_an_event_before_activation_is_refused(self):
        allowed, why = phase2_activated(
            enabled=True,
            activation_at=datetime(2026, 9, 25, 19, 30, tzinfo=UTC),
            legal_decision_window_at=OBSERVED,
        )
        assert not allowed
        assert "precedes the Phase-2 activation instant" in why

    def test_an_event_at_or_after_activation_is_allowed(self):
        allowed, why = phase2_activated(
            enabled=True, activation_at=OBSERVED, legal_decision_window_at=OBSERVED
        )
        assert allowed
        assert why == ""

    def test_disabled_is_refused_even_with_an_activation_instant(self):
        allowed, why = phase2_activated(
            enabled=False, activation_at=OBSERVED, legal_decision_window_at=OBSERVED
        )
        assert not allowed
        assert "not enabled" in why


class TestPhase2EntryEvidence:
    """Part N. Phase 2 selects structures the control never constructed, so
    the entry path has to be able to read the challenger's OWN frozen legs.
    Before this, every Phase-2 entry would have been recorded NOT_EXECUTABLE
    with 'no frozen control legs' -- an honest failure, and a total one."""

    def _frozen(self, db_session, control):
        from models.v4_2_challenger import V42ChallengerDecision  # noqa: PLC0415

        result = freeze_phase2_decision(
            db_session, control, _evaluation(), settlement_date=date(2026, 9, 25)
        )
        db_session.flush()
        return db_session.get(V42ChallengerDecision, result.decision_id)

    def test_entries_are_built_from_the_challengers_own_legs(self, db_session, control):
        from models.v4_2_challenger import V42ChallengerConfigEntry  # noqa: PLC0415
        from services.v4_2_challenger_entry import freeze_challenger_entries  # noqa: PLC0415

        challenger = self._frozen(db_session, control)
        summary = freeze_challenger_entries(
            db_session, challenger=challenger, settlement_date=date(2026, 9, 25)
        )
        db_session.flush()

        entries = (
            db_session.query(V42ChallengerConfigEntry)
            .filter_by(challenger_decision_id=challenger.id)
            .all()
        )
        assert len(entries) == 6
        assert summary.no_action == 0
        assert {e.status for e in entries} == {"OBSERVED"}
        assert len({e.candidate_id for e in entries}) == 2

    def test_each_entry_carries_its_own_expiry_and_size(self, db_session, control):
        from models.v4_2_challenger import V42ChallengerConfigEntry  # noqa: PLC0415
        from services.v4_2_challenger_entry import freeze_challenger_entries  # noqa: PLC0415

        challenger = self._frozen(db_session, control)
        freeze_challenger_entries(
            db_session, challenger=challenger, settlement_date=date(2026, 9, 25)
        )
        db_session.flush()

        rows = {
            e.configuration_key: e
            for e in db_session.query(V42ChallengerConfigEntry).filter_by(
                challenger_decision_id=challenger.id
            )
        }
        near = rows["v4_2k_conservative"]
        far = rows["v4_10k_aggressive"]
        assert near.expiration == EXPIRY_NEAR
        assert far.expiration == EXPIRY_FAR
        assert near.dte_at_settlement == 0
        assert far.dte_at_settlement == 21
        assert near.quantity and far.quantity
        assert near.capital_used > 0, "a real debit was recorded as free"
        assert far.max_risk_used > 0

    def test_leg_provenance_says_the_challenger_carried_the_quote(self, db_session, control):
        from models.v4_2_challenger import V42ChallengerCandidateObservation  # noqa: PLC0415
        from services.v4_2_challenger_entry import freeze_challenger_entries  # noqa: PLC0415

        challenger = self._frozen(db_session, control)
        freeze_challenger_entries(
            db_session, challenger=challenger, settlement_date=date(2026, 9, 25)
        )
        db_session.flush()

        observations = (
            db_session.query(V42ChallengerCandidateObservation)
            .filter_by(challenger_decision_id=challenger.id)
            .all()
        )
        assert len(observations) == 2
        for obs in observations:
            legs = obs.legs_json["legs"]
            assert legs
            assert {leg["source"] for leg in legs} == {"challenger_frozen_entry_quote"}
            # Buys pay ASK, sells receive BID. No midpoint, ever.
            for leg in legs:
                if leg["action"] == "buy":
                    assert leg["required_side"] == "ask"
                    assert leg["price"] == leg["ask"]
                else:
                    assert leg["required_side"] == "bid"
                    assert leg["price"] == leg["bid"]
            # The control never built these, so nothing was reused from it.
            assert obs.contracts_shared_with_control == 0
