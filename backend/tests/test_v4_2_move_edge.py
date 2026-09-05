"""V4.2 -- the quantitative move-edge test and its explicit diagnostics.

The question this answers is market-relative, not absolute: what move does
the history support RELATIVE to what the option market already prices? A
qualitative "large move" view is not an edge if the market implies a larger
one, and these pin that.

Applicability is derived from the project's own payoff-shape taxonomy, so
these tests never assert on a strategy name as a preference -- only that a
structure whose payoff is move-magnitude exposed is judged on move magnitude
and one whose payoff is not, is not.
"""

from datetime import date
from decimal import Decimal

import pytest

from analytics.decision.v4_2_viability import (
    MOVE_EDGE_FAIL,
    MOVE_EDGE_INSUFFICIENT,
    MOVE_EDGE_NOT_APPLICABLE,
    MOVE_EDGE_PASS,
    MoveEvidence,
    ViabilityPolicy,
    evaluate_move_edge,
    move_exposure_for,
)
from analytics.earnings.v4_2_move_distribution import build_move_distribution

D = Decimal


def _ev(implied: str, median_abs: str | None, n: int = 20) -> MoveEvidence:
    moves = (
        [] if median_abs is None
        else [D(median_abs) if i % 2 else -D(median_abs) for i in range(n)]
    )
    return MoveEvidence(
        implied_move_pct=D(implied),
        distribution=build_move_distribution(moves, as_of=date(2026, 9, 3)),
    )


class TestExposureComesFromPayoffShape:
    @pytest.mark.parametrize("strategy", ["long_straddle", "long_strangle"])
    def test_two_sided_convex_structures_are_long_move(self, strategy):
        assert move_exposure_for(strategy) == "long"

    @pytest.mark.parametrize("strategy", ["iron_condor", "iron_butterfly", "long_call_butterfly"])
    def test_range_and_pinning_structures_are_short_move(self, strategy):
        assert move_exposure_for(strategy) == "short"

    @pytest.mark.parametrize("strategy", [
        "long_call", "long_put", "bull_call_spread", "bear_put_spread",
        "put_credit_spread", "call_credit_spread",
    ])
    def test_directional_and_bounded_structures_are_not_move_magnitude_exposed(self, strategy):
        """A single-sided convex payoff needs a DIRECTIONAL move past its own
        breakeven, and a bounded vertical is threshold-shaped; neither is
        judged by a two-sided magnitude test."""
        assert move_exposure_for(strategy) is None

    def test_an_unknown_family_is_not_silently_assumed_neutral(self):
        assert move_exposure_for("not_a_real_strategy") is None


class TestLongMoveEdge:
    def test_history_materially_above_implied_passes(self):
        out = evaluate_move_edge("long_strangle", _ev("0.10", "0.15"))
        assert out.status == MOVE_EDGE_PASS
        assert out.edge_ratio == D("1.5")
        assert out.exposure == "long"

    def test_history_below_implied_fails(self):
        """The market already prices more move than history supports."""
        out = evaluate_move_edge("long_strangle", _ev("0.20", "0.12"))
        assert out.status == MOVE_EDGE_FAIL

    def test_a_thin_edge_inside_the_margin_fails(self):
        """5% above implied is not 'materially' above it at these samples."""
        out = evaluate_move_edge("long_strangle", _ev("0.120", "0.126"))
        assert out.status == MOVE_EDGE_FAIL
        assert out.edge_ratio is not None and out.edge_ratio < out.threshold

    def test_exactly_at_the_threshold_passes(self):
        out = evaluate_move_edge("long_strangle", _ev("0.10", "0.12"))
        assert out.status == MOVE_EDGE_PASS


class TestShortMoveEdge:
    def test_history_materially_below_implied_passes(self):
        out = evaluate_move_edge("iron_butterfly", _ev("0.10", "0.05"))
        assert out.status == MOVE_EDGE_PASS
        assert out.exposure == "short"

    def test_history_near_implied_fails(self):
        out = evaluate_move_edge("iron_butterfly", _ev("0.10", "0.095"))
        assert out.status == MOVE_EDGE_FAIL

    def test_history_above_implied_fails(self):
        out = evaluate_move_edge("iron_condor", _ev("0.10", "0.18"))
        assert out.status == MOVE_EDGE_FAIL


class TestInsufficientEvidence:
    def test_no_history_is_insufficient_not_a_pass(self):
        out = evaluate_move_edge("long_strangle", _ev("0.10", None))
        assert out.status == MOVE_EDGE_INSUFFICIENT
        assert out.sample_n == 0

    def test_no_implied_move_is_insufficient(self):
        evidence = MoveEvidence(
            implied_move_pct=None,
            distribution=build_move_distribution([D("0.1")], as_of=date(2026, 9, 3)),
        )
        assert evaluate_move_edge("long_strangle", evidence).status == MOVE_EDGE_INSUFFICIENT

    def test_insufficient_evidence_blocks_rather_than_defaults_open(self):
        assert evaluate_move_edge("long_strangle", _ev("0.10", None)).blocking


class TestDiagnosticsAreExplicit:
    def test_every_result_carries_its_inputs_and_threshold(self):
        out = evaluate_move_edge("long_strangle", _ev("0.10", "0.15"))
        assert out.implied_move_pct == D("0.10")
        assert out.expected_abs_move_pct == D("0.15")
        assert out.threshold == D("1.20")
        assert out.sample_n == 20
        assert out.quality is not None
        assert out.version.startswith("v4_2_move_edge")

    def test_the_explanation_states_the_comparison_made(self):
        out = evaluate_move_edge("iron_butterfly", _ev("0.10", "0.05"))
        assert "short-move" in out.explanation
        assert "implied" in out.explanation

    def test_exceedance_is_reported_as_a_diagnostic(self):
        out = evaluate_move_edge("long_strangle", _ev("0.10", "0.15"))
        assert out.exceedance_of_implied == D("1")

    def test_a_not_applicable_result_explains_why_rather_than_passing_silently(self):
        out = evaluate_move_edge("bull_call_spread", _ev("0.10", "0.15"))
        assert out.status == MOVE_EDGE_NOT_APPLICABLE
        assert not out.blocking
        assert "not the right instrument" in out.explanation

    def test_the_gate_can_be_isolated_for_sensitivity_reporting(self):
        policy = ViabilityPolicy(require_move_edge=False)
        out = evaluate_move_edge("long_strangle", _ev("0.10", None), policy)
        assert out.status == MOVE_EDGE_NOT_APPLICABLE
        assert "sensitivity" in out.explanation
