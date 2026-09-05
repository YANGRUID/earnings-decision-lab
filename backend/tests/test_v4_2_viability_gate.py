"""V4.2 challenger -- the absolute economic viability gate.

These pin BEHAVIOUR, never strategy names or realized outcomes. No test here
asserts that a particular historical trade would have been avoided: the gate
is an ex-ante economic judgement and is tested as one.
"""

from datetime import date
from decimal import Decimal

import pytest

from analytics.decision.v4_2_viability import (
    INSUFFICIENT_MOVE_EVIDENCE,
    NEGATIVE_MEDIAN,
    NO_MOVE_EDGE,
    NO_POSITIVE_SCENARIOS,
    NO_PROFITABLE_REGION,
    SPREAD_UNACCEPTABLE,
    WORST_CASE_UNACCEPTABLE,
    CandidateEconomics,
    MoveEvidence,
    ViabilityPolicy,
    assess_viability,
    choose_v4_2_candidate,
)
from analytics.earnings.v4_2_move_distribution import build_move_distribution

D = Decimal


def econ(cid="c1", strategy="bull_call_spread", median="0.05", worst="-0.10",
         best="0.30", pos="0.50", nopro=False, sem="1.0", spread="0.05"):
    return CandidateEconomics(
        candidate_id=cid, strategy=strategy,
        median_return=D(median), worst_return=D(worst), best_return=D(best),
        positive_scenario_fraction=D(pos), no_profitable_region=nopro,
        semantic_compatibility=D(sem), mean_relative_spread=D(spread),
    )


def _evidence(implied: str, moves: list[str]) -> MoveEvidence:
    """Builds evidence through the REAL distribution object, so these tests
    exercise the same construction the replay and production paths use --
    never a stand-in with hand-set fields."""
    return MoveEvidence(
        implied_move_pct=D(implied),
        distribution=build_move_distribution(
            [D(m) for m in moves], as_of=date(2026, 9, 1)
        ),
    )


#: Historical median move well ABOVE implied -- a long-move edge exists.
LONG_EDGE = _evidence("0.10", ["0.15", "-0.16", "0.14", "-0.15", "0.15", "0.15"])
#: Historical median move well BELOW implied -- a short-move edge exists.
SHORT_EDGE = _evidence("0.10", ["0.05", "-0.04", "0.05", "-0.06", "0.05", "0.05"])
#: Exactly the production situation: no historical distribution at all.
NO_EVIDENCE = _evidence("0.10", [])


class TestAbsoluteGate:
    def test_an_economically_sound_candidate_is_accepted(self):
        assert assess_viability(econ(), LONG_EDGE).acceptable

    def test_a_negative_median_is_refused(self):
        v = assess_viability(econ(median="-0.02"), LONG_EDGE)
        assert not v.acceptable
        assert NEGATIVE_MEDIAN in v.reason_codes

    def test_a_zero_median_is_refused(self):
        """Not above the bar is not over the bar."""
        assert not assess_viability(econ(median="0"), LONG_EDGE).acceptable

    def test_no_profitable_region_is_refused(self):
        v = assess_viability(econ(nopro=True, best="-0.02"), LONG_EDGE)
        assert not v.acceptable
        assert NO_PROFITABLE_REGION in v.reason_codes

    def test_a_negative_best_case_is_refused_even_without_the_flag(self):
        v = assess_viability(econ(best="-0.01", nopro=False), LONG_EDGE)
        assert NO_PROFITABLE_REGION in v.reason_codes

    def test_zero_positive_scenarios_is_refused(self):
        v = assess_viability(econ(pos="0"), LONG_EDGE)
        assert NO_POSITIVE_SCENARIOS in v.reason_codes

    def test_an_unacceptable_worst_case_is_refused(self):
        v = assess_viability(econ(worst="-0.60"), LONG_EDGE)
        assert WORST_CASE_UNACCEPTABLE in v.reason_codes

    def test_round_trip_spread_that_dominates_the_edge_is_refused(self):
        v = assess_viability(econ(spread="0.40"), LONG_EDGE)
        assert SPREAD_UNACCEPTABLE in v.reason_codes

    def test_a_candidate_that_could_not_be_valued_is_never_acceptable(self):
        broken = CandidateEconomics("c", "long_call", None, None, None, None, None, None, None)
        assert not assess_viability(broken, LONG_EDGE).acceptable

    def test_every_failure_names_itself(self):
        v = assess_viability(econ(median="-0.05", pos="0", worst="-0.9"), LONG_EDGE)
        assert len(v.reason_codes) >= 3
        assert all(isinstance(c, str) and c for c in v.reason_codes)


class TestNoActionIsAFirstClassOutcome:
    def test_all_candidates_negative_gives_no_action(self):
        out = choose_v4_2_candidate(
            [econ("a", median="-0.01"), econ("b", median="-0.05"), econ("c", median="-0.20")],
            LONG_EDGE,
        )
        assert out.status == "NO_ACTION"
        assert out.selected_candidate_id is None
        assert NEGATIVE_MEDIAN in (out.no_action_reason or "")

    def test_all_candidates_without_a_profitable_region_gives_no_action(self):
        out = choose_v4_2_candidate(
            [econ("a", nopro=True, best="-0.01"), econ("b", nopro=True, best="-0.03")],
            LONG_EDGE,
        )
        assert out.status == "NO_ACTION"

    def test_one_economically_strong_candidate_is_selected(self):
        out = choose_v4_2_candidate(
            [
                econ("bad", median="-0.10"),
                econ("good", median="0.08"),
                econ("worse", median="-0.30"),
            ],
            LONG_EDGE,
        )
        assert out.status == "RANKED"
        assert out.selected_candidate_id == "good"

    def test_the_no_action_reason_counts_why_each_candidate_failed(self):
        out = choose_v4_2_candidate([econ("a", median="-0.01"), econ("b", pos="0")], LONG_EDGE)
        assert "NEGATIVE_MEDIAN" in (out.no_action_reason or "")

    def test_an_empty_candidate_set_is_no_action_not_a_crash(self):
        assert choose_v4_2_candidate([], LONG_EDGE).status == "NO_ACTION"


class TestEconomicsOutranksSemanticBand:
    def test_the_better_modeled_median_wins(self):
        """V4.1 sorts semantics first, lexicographically, so a weaker
        economic candidate in a higher semantic band always won. Here
        economics decides among candidates that already cleared the gate."""
        out = choose_v4_2_candidate(
            [econ("strong_semantics_weak_econ", sem="1.0", median="0.01"),
             econ("weaker_semantics_strong_econ", sem="0.5", median="0.09")],
            LONG_EDGE,
        )
        assert out.selected_candidate_id == "weaker_semantics_strong_econ"

    def test_a_semantic_contradiction_is_still_refused_outright(self):
        out = choose_v4_2_candidate([econ("contradiction", sem="0.0", median="0.50")], LONG_EDGE)
        assert out.status == "NO_ACTION"


class TestMoveSemantics:
    def test_a_long_move_structure_needs_expected_move_above_implied(self):
        ok = assess_viability(econ(strategy="long_strangle"), LONG_EDGE)
        assert ok.acceptable

    def test_a_long_move_structure_is_refused_when_the_market_prices_more(self):
        """A 'large move' view is not an edge if the option market already
        implies a larger one."""
        priced_in = _evidence("0.20", ["0.12", "-0.12", "0.12", "-0.13", "0.12", "0.12"])
        v = assess_viability(econ(strategy="long_strangle"), priced_in)
        assert not v.acceptable
        assert NO_MOVE_EDGE in v.reason_codes

    def test_a_short_move_structure_needs_expected_move_below_implied(self):
        assert assess_viability(econ(strategy="iron_butterfly"), SHORT_EDGE).acceptable

    def test_a_short_move_structure_is_refused_without_a_real_margin(self):
        marginal = _evidence("0.10", ["0.095", "-0.095", "0.095", "-0.096", "0.095", "0.095"])
        v = assess_viability(econ(strategy="iron_butterfly"), marginal)
        assert NO_MOVE_EDGE in v.reason_codes

    def test_a_qualitative_label_alone_is_not_an_edge(self):
        """The production situation: no historical move distribution exists,
        so no quantitative expected move can be derived. The gate refuses the
        move-exposed structure rather than accepting the label."""
        for strategy in ("long_strangle", "iron_butterfly"):
            v = assess_viability(econ(strategy=strategy), NO_EVIDENCE)
            assert not v.acceptable
            assert INSUFFICIENT_MOVE_EVIDENCE in v.reason_codes

    def test_a_directional_structure_is_not_judged_on_move_edge(self):
        """This gate makes no claim about direction, so a vertical or a
        single option is not refused for lacking a move edge."""
        for strategy in ("bull_call_spread", "long_call", "long_put"):
            assert assess_viability(econ(strategy=strategy), NO_EVIDENCE).acceptable

    def test_the_move_edge_requirement_can_be_isolated_for_sensitivity(self):
        policy = ViabilityPolicy(require_move_edge=False)
        assert assess_viability(econ(strategy="long_strangle"), NO_EVIDENCE, policy).acceptable


class TestPolicyIsExplicit:
    @pytest.mark.parametrize("median,accepted", [("0.001", True), ("0", False), ("-0.001", False)])
    def test_the_median_bar_is_exactly_zero_by_default(self, median, accepted):
        assert assess_viability(econ(median=median), LONG_EDGE).acceptable is accepted

    def test_a_stricter_median_bar_refuses_a_thin_edge(self):
        policy = ViabilityPolicy(min_median_return=D("0.05"))
        assert not assess_viability(econ(median="0.02"), LONG_EDGE, policy).acceptable

    def test_the_verdict_carries_its_gate_version(self):
        assert assess_viability(econ(), LONG_EDGE).gate_version.startswith("v4_2_viability_gate")
