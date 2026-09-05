"""Frozen ex-ante regressions against real V4.1 candidate sets.

Every number below is a value V4.1 had already computed and persisted on
``v4_shadow_candidate`` at the moment the real decision was made. Nothing
here is a realized outcome, and no test asserts which candidate turned out
profitable -- these pin ORDERING AND ELIGIBILITY PRINCIPLES, so they would
still be correct had the market gone the other way.

They also deliberately never name a preferred strategy family. The
assertions are about economics and modeled scenario surfaces; the family
names appear only because the fixtures are real.
"""

from datetime import date
from decimal import Decimal

from analytics.decision.v4_2_viability import (
    NO_PROFITABLE_REGION,
    CandidateEconomics,
    MoveEvidence,
    ViabilityPolicy,
    assess_viability,
    choose_v4_2_candidate,
)
from analytics.earnings.v4_2_move_distribution import build_move_distribution

D = Decimal


def _evidence(implied: str, median_abs: str, n: int = 24) -> MoveEvidence:
    """A distribution whose median magnitude is ``median_abs`` -- built
    through the real object so quality tiers apply as in production."""
    moves = [D(median_abs) if i % 2 else -D(median_abs) for i in range(n)]
    return MoveEvidence(
        implied_move_pct=D(implied),
        distribution=build_move_distribution(moves, as_of=date(2026, 9, 3)),
    )


def _c(cid, strategy, median, worst, best, pos, nopro, sem, spread):
    return CandidateEconomics(
        candidate_id=cid, strategy=strategy, median_return=D(median),
        worst_return=D(worst), best_return=D(best),
        positive_scenario_fraction=D(pos), no_profitable_region=nopro,
        semantic_compatibility=D(sem), mean_relative_spread=D(spread),
    )


# --------------------------------------------------------------------------
# GWRE, 2026-09-03. V4.1 ranked the iron_condor first on a higher semantic
# band; the candidate with the only positive modeled median sat at rank 8
# because its semantic band was lower. Implied move 13.50%, historical
# median magnitude 5.03% over 24 point-in-time observations.
# --------------------------------------------------------------------------
GWRE_CANDIDATES = [
    _c("iron_condor:NARROWER_RANGE", "iron_condor",
       "-0.082233", "-0.256306", "0.039228", "0.142857", False, "0.500000", "0.119149"),
    _c("iron_butterfly:WIDER_RANGE", "iron_butterfly",
       "-0.157052", "-0.342768", "0.171477", "0.285714", False, "0.500000", "0.113279"),
    _c("call_credit_spread:WIDER_WING", "call_credit_spread",
       "0.042096", "-0.272658", "0.127009", "0.571429", False, "0.250000", "0.112836"),
]
GWRE_EVIDENCE = _evidence("0.135", "0.0503", n=24)


class TestSemanticBandDoesNotBuryBetterEconomics:
    def test_the_higher_semantic_band_does_not_automatically_win(self):
        """The V4.1 behaviour this challenger exists to correct: a
        lexicographic semantic-first key made the 0.50-band candidate
        unbeatable regardless of the economic gap."""
        out = choose_v4_2_candidate(GWRE_CANDIDATES, GWRE_EVIDENCE)
        assert out.status == "RANKED"
        winner = next(c for c in GWRE_CANDIDATES if c.candidate_id == out.selected_candidate_id)
        loser = next(c for c in GWRE_CANDIDATES if c.candidate_id == "iron_condor:NARROWER_RANGE")
        assert winner.median_return > loser.median_return
        assert winner.semantic_compatibility <= loser.semantic_compatibility, (
            "this fixture is only meaningful while the better-economics candidate "
            "sits in the LOWER semantic band"
        )

    def test_a_negative_median_candidate_is_not_selected_when_a_positive_one_qualifies(self):
        out = choose_v4_2_candidate(GWRE_CANDIDATES, GWRE_EVIDENCE)
        winner = next(c for c in GWRE_CANDIDATES if c.candidate_id == out.selected_candidate_id)
        assert winner.median_return > 0

    def test_semantics_still_gates_a_contradiction_out(self):
        """Economics-first is not economics-only: a contradictory candidate
        is refused however good its modelled numbers are."""
        contradiction = _c("contradiction", "call_credit_spread",
                           "0.50", "-0.05", "0.90", "0.90", False, "0.0", "0.02")
        out = choose_v4_2_candidate([contradiction], GWRE_EVIDENCE)
        assert out.status == "NO_ACTION"


# --------------------------------------------------------------------------
# DOCU and ZS, 2026-09-03. Both were actioned by V4.1 with candidates whose
# own modeled scenario surface contained no profitable state at all.
# --------------------------------------------------------------------------
DOCU_CANDIDATES = [
    _c("iron_butterfly:WIDER_RANGE", "iron_butterfly",
       "-0.054565", "-0.084864", "-0.021235", "0.000000", True, "0.500000", "0.110041"),
    _c("long_call_butterfly:WIDER_RANGE", "long_call_butterfly",
       "-0.049350", "-0.129616", "-0.019793", "0.000000", True, "0.500000", "0.089652"),
    _c("long_straddle:BASE", "long_straddle",
       "-0.019284", "-0.188635", "0.107375", "0.333333", False, "0.500000", "0.083080"),
]
ZS_CANDIDATES = [
    _c("iron_butterfly:WIDER_RANGE", "iron_butterfly",
       "-0.170272", "-0.226826", "-0.018600", "0.000000", True, "1.000000", "0.056771"),
    _c("long_call_butterfly:WIDER_RANGE", "long_call_butterfly",
       "-0.216487", "-0.438480", "-0.096090", "0.000000", True, "1.000000", "0.059851"),
    _c("bull_call_spread:FULL_MOVE_SHORT", "bull_call_spread",
       "-0.075060", "-0.250550", "0.139828", "0.190476", False, "0.250000", "0.044041"),
]


class TestNoProfitableRegionCanNeverBeActioned:
    """Tautological model consistency, not a reaction to what these two
    positions went on to lose: if the modeled surface holds no profitable
    state, the position must not be opened."""

    def test_the_docu_selection_is_refused_for_having_no_profitable_region(self):
        selected = DOCU_CANDIDATES[0]
        verdict = assess_viability(selected, _evidence("0.1092", "0.0743", n=16))
        assert not verdict.acceptable
        assert NO_PROFITABLE_REGION in verdict.reason_codes

    def test_the_zs_selection_is_refused_for_having_no_profitable_region(self):
        selected = ZS_CANDIDATES[0]
        verdict = assess_viability(selected, _evidence("0.1404", "0.0960", n=14))
        assert not verdict.acceptable
        assert NO_PROFITABLE_REGION in verdict.reason_codes

    def test_a_set_where_every_candidate_is_modelled_to_lose_gives_no_action(self):
        out = choose_v4_2_candidate(DOCU_CANDIDATES, _evidence("0.1092", "0.0743", n=16))
        assert out.status == "NO_ACTION"
        assert out.selected_candidate_id is None

    def test_the_refusal_is_explained_by_code_not_by_strategy_name(self):
        out = choose_v4_2_candidate(ZS_CANDIDATES, _evidence("0.1404", "0.0960", n=14))
        assert out.status == "NO_ACTION"
        reason = out.no_action_reason or ""
        assert "NEGATIVE_MEDIAN" in reason or "NO_PROFITABLE_REGION" in reason
        for family in ("iron_butterfly", "bull_call_spread", "long_call_butterfly"):
            assert family not in reason, "a refusal must never single out a strategy family"

    def test_the_minimal_tautological_rule_holds_without_any_calibration(self):
        """With every other bound relaxed to the point of being inert, a
        no-profitable-region candidate is STILL refused."""
        inert = ViabilityPolicy(
            require_move_edge=False, min_median_return=D("-9"),
            max_worst_case_loss=D("9"), max_mean_relative_spread=D("9"),
            min_semantic_compatibility=D("-9"),
        )
        for candidate in (DOCU_CANDIDATES[0], ZS_CANDIDATES[0]):
            verdict = assess_viability(candidate, GWRE_EVIDENCE, inert)
            assert not verdict.acceptable
            assert NO_PROFITABLE_REGION in verdict.reason_codes

    def test_that_same_inert_policy_still_admits_a_candidate_with_a_profitable_region(self):
        """Proves the previous test isolates the tautological rule rather
        than simply rejecting everything."""
        inert = ViabilityPolicy(
            require_move_edge=False, min_median_return=D("-9"),
            max_worst_case_loss=D("9"), max_mean_relative_spread=D("9"),
            min_semantic_compatibility=D("-9"),
        )
        assert assess_viability(DOCU_CANDIDATES[2], GWRE_EVIDENCE, inert).acceptable
