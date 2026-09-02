"""V4.4B -- synthetic economic test matrix (Section 29) plus validity and
explainability coverage.

DESIGN RULE, taken directly from Section 29: *"Do not hardcode strategy
names into rank results merely to make tests pass. Tests should arise
from economics."* Every ordering asserted below is therefore driven by
the numbers on the candidate's own T+1 scenario surface and quotes --
never by which strategy family it happens to belong to. A butterfly loses
here only when its own modeled outcomes collapse outside a narrow region;
swap the economics and the same strategy name wins.

The distribution statistics are produced by the REAL
``summarize_candidate_distribution`` from V4.4A, not a stub, so these
tests exercise the genuine median/worst/coverage math the ranker consumes.
"""

from datetime import UTC, date, datetime
from decimal import Decimal

from analytics.decision.v4_4b_ranking import (
    RANKING_VERSION,
    RankableCandidate,
    assess_execution_quality,
    assess_robustness,
    build_ranking_key,
    classify_candidate_validity,
    explain_pairwise,
    rank_candidates,
)
from analytics.decision.v4_compatibility import SemanticCompatibilityResult
from analytics.decision.v4_expected_move import ExpectedMoveContext
from analytics.decision.v4_t1_pricing import (
    T1ScenarioResult,
    summarize_candidate_distribution,
)
from analytics.decision.v4_t1_valuation_context import V4T1LegInput, V4T1ValuationContext

NOW = datetime(2026, 9, 1, 14, 0, tzinfo=UTC)
EXPIRATION = date(2026, 9, 18)

UNDERLYING_LABELS = (
    ("LARGE_DOWNSIDE", Decimal("-1")),
    ("MODERATE_DOWNSIDE", Decimal("-0.5")),
    ("SMALL_DOWNSIDE", Decimal("-0.25")),
    ("FLAT", Decimal("0")),
    ("SMALL_UPSIDE", Decimal("0.25")),
    ("MODERATE_UPSIDE", Decimal("0.5")),
    ("LARGE_UPSIDE", Decimal("1")),
)
IV_LABELS = ("STRONG_CRUSH", "NORMAL_CRUSH", "WEAK_CRUSH_OR_ELEVATED")


# --------------------------------------------------------------------------
# Builders -- deliberately economics-first: a test states the return
# surface it wants and gets a candidate exhibiting exactly that.
# --------------------------------------------------------------------------


def _expected_move_context() -> ExpectedMoveContext:
    return ExpectedMoveContext(
        spot=Decimal("100"),
        observed_at=NOW,
        implied_move_available=True,
        implied_move_dollars=Decimal("5"),
        implied_move_pct=Decimal("0.05"),
        upper_implied_boundary=Decimal("105"),
        lower_implied_boundary=Decimal("95"),
        implied_move_source="atm_straddle",
        implied_move_result=None,
        historical_sample_n=8,
        historical_evidence_quality="adequate",
        historical_median_abs_move_pct=Decimal("0.04"),
        historical_median_upper_boundary=Decimal("104"),
        historical_median_lower_boundary=Decimal("96"),
        historical_quantiles=None,
        historical_move_stats=None,
        context_version="test",
    )


def _leg(
    index: int,
    action: str,
    right: str,
    strike: str,
    bid: str | None = "1.00",
    ask: str | None = "1.10",
    iv: str | None = "0.40",
    quality: str | None = "delayed",
) -> V4T1LegInput:
    return V4T1LegInput(
        leg_index=index,
        action=action,  # type: ignore[arg-type]
        right=right,  # type: ignore[arg-type]
        strike=Decimal(strike),
        quantity=1,
        multiplier=Decimal("100"),
        entry_bid=Decimal(bid) if bid is not None else None,
        entry_ask=Decimal(ask) if ask is not None else None,
        entry_last=None,
        entry_iv=Decimal(iv) if iv is not None else None,
        entry_delta=None,
        entry_gamma=None,
        entry_theta=None,
        entry_vega=None,
        market_data_quality=quality,
        external_contract_id=f"c{index}",
    )


def _context(strategy: str, legs: tuple[V4T1LegInput, ...]) -> V4T1ValuationContext:
    return V4T1ValuationContext(
        ticker="TEST",
        underlying_price=Decimal("100"),
        observed_at=NOW,
        entry_timestamp=NOW,
        expected_exit_timestamp=NOW,
        strategy=strategy,  # type: ignore[arg-type]
        expiration=EXPIRATION,
        legs=legs,
        expected_move_context=_expected_move_context(),
    )


def _scenarios(returns_by_move: dict[str, str], variant_id: str) -> tuple[T1ScenarioResult, ...]:
    """Builds the full 7x3 surface from a per-underlying-move return.
    Constant across IV scenarios unless a test overrides -- keeps each
    test's economic intent legible."""
    out = []
    for move_label, fraction in UNDERLYING_LABELS:
        for iv_label in IV_LABELS:
            value = returns_by_move[move_label]
            out.append(
                T1ScenarioResult(
                    variant_id=variant_id,
                    scenario_id=f"{move_label}|{iv_label}",
                    underlying_move_label=move_label,
                    underlying_move_em_fraction=fraction,
                    scenario_underlying_price=Decimal("100") + fraction * Decimal("5"),
                    iv_scenario_label=iv_label,
                    iv_scenario_multiplier=Decimal("0.75"),
                    dte_remaining_at_exit=17,
                    leg_values=(),
                    entry_cashflow=Decimal("-100"),
                    theoretical_liquidation_value=None,
                    executable_liquidation_value=None,
                    realized_equivalent_pnl_theoretical=None,
                    realized_equivalent_pnl_executable=None,
                    return_on_standardized_capital_theoretical=Decimal(value),
                    return_on_standardized_capital_executable=Decimal(value),
                    return_on_entry_cash=None,
                    reason_codes=(),
                    quality_note="synthetic",
                )
            )
    return tuple(out)


def _compat(overall: str) -> SemanticCompatibilityResult:
    v = float(overall)
    return SemanticCompatibilityResult(
        direction_compatibility=v,
        move_magnitude_compatibility=v,
        volatility_compatibility=v,
        payoff_shape_compatibility=v,
        overall_semantic_compatibility=v,
        reason_codes=(),
        explanation=f"synthetic compatibility {overall}",
    )


def _candidate(
    candidate_id: str,
    strategy: str,
    returns_by_move: dict[str, str],
    compatibility: str = "1.0",
    legs: tuple[V4T1LegInput, ...] | None = None,
    capital_utilisation: str | None = "0.10",
) -> RankableCandidate:
    legs = legs or (_leg(0, "buy", "C", "100"),)
    scenarios = _scenarios(returns_by_move, candidate_id)
    return RankableCandidate(
        candidate_id=candidate_id,
        context=_context(strategy, legs),
        scenario_results=scenarios,
        distribution=summarize_candidate_distribution(scenarios),
        semantic_compatibility=_compat(compatibility),
        entry_cash_required=Decimal("200"),
        capital_utilisation=Decimal(capital_utilisation) if capital_utilisation else None,
    )


def _flat(value: str) -> dict[str, str]:
    return {label: value for label, _ in UNDERLYING_LABELS}


#: Profits only when the underlying barely moves; collapses outside that
#: -- the economic signature of a narrow pinning structure.
def _pinned(center: str = "0.40", wings: str = "-0.30") -> dict[str, str]:
    surface = _flat(wings)
    surface["FLAT"] = center
    return surface


#: Profits on large moves either way, loses when nothing happens -- the
#: economic signature of a long-volatility structure.
def _long_vol(big: str = "0.45", small: str = "-0.12") -> dict[str, str]:
    return {
        "LARGE_DOWNSIDE": big,
        "MODERATE_DOWNSIDE": "0.10",
        "SMALL_DOWNSIDE": small,
        "FLAT": small,
        "SMALL_UPSIDE": small,
        "MODERATE_UPSIDE": "0.10",
        "LARGE_UPSIDE": big,
    }


def _rank_of(results, candidate_id: str) -> int | None:
    return next(r.rank for r in results if r.candidate_id == candidate_id)


# --------------------------------------------------------------------------
# Section 29 -- the synthetic economic matrix.
# --------------------------------------------------------------------------


class TestEconomicOrdering:
    def test_robust_structure_beats_narrow_pinning_structure(self):
        """The V3 failure mode, in miniature. Both are debits with a
        similar headline upside; the pinning structure only wins in a
        single price region and loses everywhere else. Downside and
        coverage must surface that."""
        robust = _candidate("robust", "long_straddle", _long_vol())
        pinned = _candidate("pinned", "long_call_butterfly", _pinned())
        results = rank_candidates([pinned, robust])
        assert _rank_of(results, "robust") < _rank_of(results, "pinned")

    def test_the_same_strategy_family_wins_when_its_economics_are_better(self):
        """Proves the ordering is economic, not name-based: the butterfly
        that was last above now ranks FIRST once its own surface is the
        robust one. Nothing about the strategy label changed."""
        good_fly = _candidate("fly", "long_call_butterfly", _long_vol())
        bad_straddle = _candidate("straddle", "long_straddle", _pinned())
        results = rank_candidates([bad_straddle, good_fly])
        assert _rank_of(results, "fly") < _rank_of(results, "straddle")

    def test_semantic_contradiction_cannot_outrank_on_economics_alone(self):
        """Section 5: a contradictory candidate is floored below every
        non-contradictory one even when its economics look better."""
        contradictory = _candidate(
            "contradictory", "long_call_butterfly", _flat("0.50"), compatibility="0.0"
        )
        compatible = _candidate(
            "compatible", "long_straddle", _flat("0.05"), compatibility="1.0"
        )
        results = rank_candidates([contradictory, compatible])
        assert _rank_of(results, "compatible") < _rank_of(results, "contradictory")

    def test_contradictory_candidate_is_still_ranked_not_deleted(self):
        """Section 5 again: heavily disadvantaged, never hidden."""
        contradictory = _candidate(
            "contradictory", "iron_condor", _flat("0.50"), compatibility="0.0"
        )
        results = rank_candidates([contradictory])
        row = results[0]
        assert row.rank == 1
        assert row.status == "RANKABLE"
        assert "CONTRADICTION" in row.rationale

    def test_worse_downside_loses_even_with_identical_median(self):
        """Section 17: downside must matter, and must not be averaged
        away. Both candidates share a median; one has a far worse tail."""
        safe = dict(_flat("0.10"))
        risky = dict(_flat("0.10"))
        risky["LARGE_DOWNSIDE"] = "-0.90"
        results = rank_candidates(
            [
                _candidate("risky", "long_straddle", risky),
                _candidate("safe", "long_straddle", safe),
            ]
        )
        assert _rank_of(results, "safe") < _rank_of(results, "risky")

    def test_one_huge_upside_does_not_rescue_a_disastrous_candidate(self):
        """Section 17's explicit warning: a single spectacular scenario
        must not dominate a candidate that behaves terribly elsewhere."""
        lottery = dict(_flat("-0.50"))
        lottery["LARGE_UPSIDE"] = "5.00"
        steady = _flat("0.05")
        results = rank_candidates(
            [
                _candidate("lottery", "long_call", lottery),
                _candidate("steady", "call_debit_spread", steady),
            ]
        )
        assert _rank_of(results, "steady") < _rank_of(results, "lottery")

    def test_higher_spread_friction_loses_when_everything_else_matches(self):
        """Section 9: execution quality is measured on the candidate's own
        legs, not as a binary any-quote-present flag."""
        tight = _candidate(
            "tight", "long_call", _flat("0.10"), legs=(_leg(0, "buy", "C", "100", "1.00", "1.02"),)
        )
        wide = _candidate(
            "wide", "long_call", _flat("0.10"), legs=(_leg(0, "buy", "C", "100", "0.70", "1.40"),)
        )
        results = rank_candidates([wide, tight])
        assert _rank_of(results, "tight") < _rank_of(results, "wide")

    def test_better_capital_efficiency_breaks_an_otherwise_exact_tie(self):
        cheap = _candidate("cheap", "long_call", _flat("0.10"), capital_utilisation="0.10")
        dear = _candidate("dear", "long_call", _flat("0.10"), capital_utilisation="0.80")
        results = rank_candidates([dear, cheap])
        assert _rank_of(results, "cheap") < _rank_of(results, "dear")

    def test_broader_scenario_coverage_wins_at_equal_downside_and_median(self):
        narrow = dict(_flat("-0.02"))
        for label in ("FLAT", "SMALL_UPSIDE"):
            narrow[label] = "0.02"
        broad = dict(_flat("0.02"))
        broad["LARGE_DOWNSIDE"] = "-0.02"
        results = rank_candidates(
            [
                _candidate("narrow", "iron_butterfly", narrow),
                _candidate("broad", "iron_condor", broad),
            ]
        )
        assert _rank_of(results, "broad") < _rank_of(results, "narrow")


# --------------------------------------------------------------------------
# Section 13/14 -- validity states, kept distinct from bad economics.
# --------------------------------------------------------------------------


class TestValidityStates:
    def test_missing_required_side_is_not_executable_now(self):
        """Section 11: LONG entry needs ASK. Without it the candidate is
        NOT EXECUTABLE -- a data state, not a zero score."""
        candidate = _candidate(
            "no_ask", "long_call", _flat("0.10"), legs=(_leg(0, "buy", "C", "100", ask=None),)
        )
        status, reason = classify_candidate_validity(candidate)
        assert status == "QUOTE_INCOMPLETE"
        assert "ASK for buy" in reason

    def test_short_leg_requires_bid_not_ask(self):
        candidate = _candidate(
            "no_bid", "short_put", _flat("0.10"), legs=(_leg(0, "sell", "P", "100", bid=None),)
        )
        assert classify_candidate_validity(candidate)[0] == "QUOTE_INCOMPLETE"

    def test_missing_iv_cannot_be_valued_honestly(self):
        candidate = _candidate(
            "no_iv", "long_call", _flat("0.10"), legs=(_leg(0, "buy", "C", "100", iv=None),)
        )
        assert classify_candidate_validity(candidate)[0] == "MISSING_IV"

    def test_capital_incompatible_when_entry_exceeds_standardized_capital(self):
        candidate = _candidate(
            "too_big", "long_call", _flat("0.10"), capital_utilisation="1.50"
        )
        assert classify_candidate_validity(candidate)[0] == "CAPITAL_INCOMPATIBLE"

    def test_non_rankable_candidates_are_reported_but_never_ranked(self):
        """Section 14: a missing-data candidate must not be compared
        against a fully-valued one using invented defaults."""
        good = _candidate("good", "long_call", _flat("0.10"))
        broken = _candidate(
            "broken", "long_call", _flat("9.99"), legs=(_leg(0, "buy", "C", "100", ask=None),)
        )
        results = rank_candidates([broken, good])
        by_id = {r.candidate_id: r for r in results}
        assert by_id["good"].rank == 1
        assert by_id["broken"].rank is None
        assert by_id["broken"].status == "QUOTE_INCOMPLETE"
        # Present and explained, not silently dropped.
        assert len(results) == 2
        assert by_id["broken"].ranking_key is None

    def test_a_bad_economic_candidate_is_still_rankable(self):
        """The distinction Section 13 insists on: unattractive is not the
        same as unmeasurable."""
        awful = _candidate("awful", "long_call", _flat("-0.80"))
        status, _ = classify_candidate_validity(awful)
        assert status == "RANKABLE"


# --------------------------------------------------------------------------
# Section 18 -- robustness/pinning emerges from the surface.
# --------------------------------------------------------------------------


class TestRobustnessDiagnostic:
    def test_pinning_structure_is_flagged_as_single_region(self):
        scenarios = _scenarios(_pinned(), "pin")
        diagnostic = assess_robustness(scenarios)
        assert diagnostic.profit_concentrated_in_single_region is True
        assert diagnostic.collapses_outside_flat is True
        assert diagnostic.n_positive_underlying_regions == 1

    def test_broad_structure_is_not_flagged(self):
        diagnostic = assess_robustness(_scenarios(_long_vol(), "broad"))
        assert diagnostic.profit_concentrated_in_single_region is False
        assert diagnostic.n_positive_underlying_regions > 1

    def test_never_profitable_is_distinguished_from_pin_dependent(self):
        """Found while replaying real V3 decisions: a candidate profitable
        in NO region previously reported "not concentrated", which reads
        as reassuring when it is the opposite. The two failures are
        different and must be reported differently."""
        never = assess_robustness(_scenarios(_flat("-0.20"), "never"))
        assert never.no_profitable_region is True
        assert never.profit_concentrated_in_single_region is False

        pinned = assess_robustness(_scenarios(_pinned(), "pin"))
        assert pinned.no_profitable_region is False
        assert pinned.profit_concentrated_in_single_region is True

    def test_never_profitable_is_stated_plainly_in_the_rationale(self):
        results = rank_candidates([_candidate("x", "iron_condor", _flat("-0.20"))])
        assert "NOT profitable in ANY" in results[0].rationale

    def test_robustness_is_unmeasurable_without_valued_scenarios(self):
        diagnostic = assess_robustness(())
        assert diagnostic.positive_scenario_fraction is None


# --------------------------------------------------------------------------
# Section 10/34 -- provenance, and Section 23/24 -- explainability.
# --------------------------------------------------------------------------


class TestProvenanceAndExplainability:
    def test_delayed_quality_is_preserved_and_warned_about_not_penalized(self):
        results = rank_candidates([_candidate("d", "long_call", _flat("0.10"))])
        row = results[0]
        assert row.market_data_quality == "delayed"
        assert any("delayed" in w for w in row.data_quality_warnings)
        # Warned about, but still rankable -- no silent penalty, since no
        # documented policy for one exists.
        assert row.status == "RANKABLE"

    def test_mixed_leg_quality_is_surfaced_rather_than_collapsed(self):
        legs = (
            _leg(0, "buy", "C", "100", quality="delayed"),
            _leg(1, "sell", "C", "105", quality="live"),
        )
        execution = assess_execution_quality(legs)
        assert execution.market_data_quality is not None
        assert execution.market_data_quality.startswith("mixed:")

    def test_pairwise_explanation_names_the_deciding_dimension(self):
        """Section 23: a user must be able to answer WHY #1 beat #2
        without reading code."""
        results = rank_candidates(
            [
                _candidate("a", "long_straddle", _flat("0.30"), compatibility="1.0"),
                _candidate("b", "long_straddle", _flat("0.30"), compatibility="0.0"),
            ]
        )
        by_id = {r.candidate_id: r for r in results}
        explanation = explain_pairwise(by_id["a"], by_id["b"])
        assert "semantic compatibility" in explanation
        assert "a ranks above b" in explanation

    def test_no_probability_or_expected_return_terminology_is_emitted(self):
        """Sections 7/24/25 -- the terminology rule, asserted structurally
        rather than trusted."""
        results = rank_candidates([_candidate("x", "long_call", _flat("0.10"))])
        row = results[0]
        blob = (row.rationale + row.status_reason).lower()
        for banned in ("probability", "expected return", "win rate", "confidence"):
            assert banned not in blob

    def test_scenario_average_is_reported_but_is_not_a_ranking_dimension(self):
        """Section 7: the unweighted average is available as a diagnostic
        and must not drive the order."""
        candidate = _candidate("x", "long_call", _flat("0.10"))
        results = rank_candidates([candidate])
        assert results[0].scenario_average_return is not None
        execution = assess_execution_quality(candidate.context.legs)
        robustness = assess_robustness(candidate.scenario_results)
        key = build_ranking_key(candidate, execution, robustness)
        assert results[0].ranking_key == key
        # Six dimensions, none of which is the scenario average.
        assert len(key) == 6


class TestDeterminism:
    def test_identical_inputs_produce_identical_order_regardless_of_input_order(self):
        a = _candidate("a", "long_call", _flat("0.10"))
        b = _candidate("b", "long_call", _flat("0.20"))
        c = _candidate("c", "long_call", _flat("0.05"))
        forward = [r.candidate_id for r in rank_candidates([a, b, c])]
        backward = [r.candidate_id for r in rank_candidates([c, b, a])]
        assert forward == backward

    def test_exact_ties_break_deterministically_by_identifier(self):
        results = rank_candidates(
            [
                _candidate("zzz", "long_call", _flat("0.10")),
                _candidate("aaa", "long_call", _flat("0.10")),
            ]
        )
        assert [r.candidate_id for r in results] == ["aaa", "zzz"]

    def test_ranking_version_is_frozen_and_reported(self):
        results = rank_candidates([_candidate("x", "long_call", _flat("0.10"))])
        assert results[0].ranking_version == RANKING_VERSION
        assert RANKING_VERSION == "v4-4b-t1-executable-ranking-v1"
