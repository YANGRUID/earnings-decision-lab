"""V4.2 Phase 2 -- the six configurations must be able to disagree.

These are anti-vacuity tests. Phase 1 shipped a per-configuration layer that
produced six identical answers on all 15 real forward events, and it passed
its own tests, because nothing asserted that the six CAN differ. Each test
here constructs a universe where a specific disagreement is the economically
correct outcome, and fails if the six converge anyway.

No realized outcome appears anywhere in this file. Every number is an ex-ante
modeled quantity chosen to exercise a gate.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from analytics.decision.v4_2_config_policy import (
    STAGE_CAPITAL,
    STAGE_FAMILY,
    STATUS_ACTION,
    STATUS_NO_ACTION,
    SharedCandidate,
    decide_all_configurations,
)
from analytics.decision.v4_2_viability import CandidateEconomics, MoveEvidence
from analytics.decision.v4_configurations import V4_CONFIGURATIONS
from analytics.decision.v4_strategy_semantics import get_strategy_semantics
from analytics.options.strategy_candidates import StrategyCategory

#: No historical distribution, so the move edge is INSUFFICIENT_EVIDENCE for
#: move-exposed shapes and NOT_APPLICABLE for the bounded directional ones
#: used below. Chosen so these tests exercise capital, risk and family --
#: never the move-edge rule, which has its own tests.
NO_MOVE_EVIDENCE = MoveEvidence(implied_move_pct=Decimal("0.05"), distribution=None)


def _candidate(
    candidate_id: str,
    strategy: str,
    *,
    median: str,
    worst: str = "-0.10",
    best: str = "0.40",
    positive_fraction: str = "0.55",
    spread: str = "0.05",
    entry_cash: str = "300",
    max_risk: str = "300",
    n_legs: int = 2,
    two_sided_legs: int | None = None,
    data_invalid_reason: str | None = None,
) -> SharedCandidate:
    return SharedCandidate(
        candidate_id=candidate_id,
        strategy=strategy,
        economics=CandidateEconomics(
            candidate_id=candidate_id,
            strategy=strategy,
            median_return=Decimal(median),
            worst_return=Decimal(worst),
            best_return=Decimal(best),
            positive_scenario_fraction=Decimal(positive_fraction),
            no_profitable_region=False,
            semantic_compatibility=Decimal("0.90"),
            mean_relative_spread=Decimal(spread),
        ),
        entry_cash_required=Decimal(entry_cash),
        per_contract_max_risk=Decimal(max_risk),
        n_legs=n_legs,
        n_legs_with_two_sided_quote=n_legs if two_sided_legs is None else two_sided_legs,
        data_invalid_reason=data_invalid_reason,
    )


def _by_key(decisions):
    return {d.configuration.key: d for d in decisions}


class TestTheSixCanSelectDifferentCandidates:
    """Section 76."""

    def test_six_configurations_do_not_all_converge_on_one_candidate(self):
        """Each configuration's binding constraint admits a different best
        structure. A layer that returns one event-level winner and then asks
        who can afford it cannot produce this."""
        universe = [
            # Best economics of all, and only $10K Aggressive's $5,000 cap
            # can hold it.
            _candidate("A", "bull_call_spread", median="0.30", entry_cash="4200",
                       max_risk="4200"),
            # Next best; fits $10K Moderate's $3,000 cap.
            _candidate("B", "bear_put_spread", median="0.25", entry_cash="2400",
                       max_risk="2400"),
            # Fits $10K Conservative's $1,500 cap.
            _candidate("C", "put_credit_spread", median="0.20", entry_cash="1200",
                       max_risk="1200"),
            # Fits $2K Aggressive's $1,000 cap.
            _candidate("D", "call_credit_spread", median="0.15", entry_cash="900",
                       max_risk="900"),
            # Fits $2K Moderate's $600 cap.
            _candidate("E", "bull_call_spread", median="0.10", entry_cash="550",
                       max_risk="550"),
            # Fits $2K Conservative's $300 cap.
            _candidate("F", "bear_put_spread", median="0.05", entry_cash="250",
                       max_risk="250"),
        ]

        decided = _by_key(
            decide_all_configurations(universe, V4_CONFIGURATIONS, evidence=NO_MOVE_EVIDENCE)
        )

        assert decided["v4_2k_conservative"].selected_candidate_id == "F"
        assert decided["v4_2k_moderate"].selected_candidate_id == "E"
        assert decided["v4_2k_aggressive"].selected_candidate_id == "D"
        assert decided["v4_10k_conservative"].selected_candidate_id == "C"
        assert decided["v4_10k_moderate"].selected_candidate_id == "B"
        assert decided["v4_10k_aggressive"].selected_candidate_id == "A"

        chosen = {d.selected_candidate_id for d in decided.values()}
        assert len(chosen) == 6, "the six configurations collapsed onto fewer structures"

    def test_every_configuration_records_its_own_rejection_census(self):
        universe = [
            _candidate("A", "bull_call_spread", median="0.30", entry_cash="4200",
                       max_risk="4200"),
            _candidate("F", "bear_put_spread", median="0.05", entry_cash="250",
                       max_risk="250"),
        ]

        for decision in decide_all_configurations(
            universe, V4_CONFIGURATIONS, evidence=NO_MOVE_EVIDENCE
        ):
            diagnostics = decision.diagnostics
            assert diagnostics.universe_count == 2
            assert diagnostics.accounted_for == diagnostics.universe_count, (
                f"{decision.configuration.key} lost a candidate between stages: "
                f"{diagnostics.as_dict()}"
            )


class TestCapitalSearchContinues:
    """Section 77 -- the specific failure this architecture exists to stop."""

    def test_an_unaffordable_best_candidate_does_not_force_no_action(self):
        """Candidate A has the best economics and is far beyond $2K
        Conservative's $300 cap. B is slightly worse and fits. Conservative
        must take B, not decline the event."""
        universe = [
            _candidate("A", "bull_call_spread", median="0.40", entry_cash="1200",
                       max_risk="1200"),
            _candidate("B", "bear_put_spread", median="0.18", entry_cash="280",
                       max_risk="280"),
        ]

        decided = _by_key(
            decide_all_configurations(universe, V4_CONFIGURATIONS, evidence=NO_MOVE_EVIDENCE)
        )

        conservative = decided["v4_2k_conservative"]
        assert conservative.status == STATUS_ACTION
        assert conservative.selected_candidate_id == "B"
        assert conservative.diagnostics.risk_rejected_count == 1
        assert conservative.diagnostics.rankable_count == 1

        # And the configuration that CAN hold A still takes A.
        assert decided["v4_10k_aggressive"].selected_candidate_id == "A"

    def test_capital_and_risk_are_reported_as_distinct_refusals(self):
        """A credit structure whose debit is zero but whose defined loss is
        large is a RISK refusal, not a capital one. Reporting it as capital
        is what made an earlier refusal read as a budget problem."""
        universe = [
            _candidate("CREDIT", "put_credit_spread", median="0.12", entry_cash="0",
                       max_risk="1800"),
            _candidate("SMALL", "bear_put_spread", median="0.08", entry_cash="200",
                       max_risk="200"),
        ]

        decided = _by_key(
            decide_all_configurations(universe, V4_CONFIGURATIONS, evidence=NO_MOVE_EVIDENCE)
        )

        conservative = decided["v4_2k_conservative"]
        assert conservative.diagnostics.capital_rejected_count == 0
        assert conservative.diagnostics.risk_rejected_count == 1
        assert conservative.selected_candidate_id == "SMALL"
        assert decided["v4_10k_moderate"].selected_candidate_id == "CREDIT"


class TestSameCandidateDifferentSize:
    """Section 78."""

    def test_two_configurations_may_share_a_structure_and_size_it_differently(self):
        universe = [
            _candidate("ONE", "bull_call_spread", median="0.22", entry_cash="200",
                       max_risk="200"),
        ]

        decided = _by_key(
            decide_all_configurations(universe, V4_CONFIGURATIONS, evidence=NO_MOVE_EVIDENCE)
        )

        two_k = decided["v4_2k_moderate"]
        ten_k = decided["v4_10k_moderate"]
        assert two_k.selected_candidate_id == ten_k.selected_candidate_id == "ONE"
        assert two_k.position is not None and ten_k.position is not None
        # $600 cap / $200 risk = 3 contracts; $3,000 / $200 = 15.
        assert two_k.position.quantity == 3
        assert ten_k.position.quantity == 15
        assert ten_k.position.quantity > two_k.position.quantity
        assert ten_k.position.max_risk_used <= ten_k.configuration.max_risk_dollars
        assert two_k.position.max_risk_used <= two_k.configuration.max_risk_dollars


class TestAllSixDecline:
    """Section 79 -- NO_ACTION stays reachable and stays honest."""

    def test_a_universe_that_fails_absolute_economics_declines_everywhere(self):
        universe = [
            _candidate("LOSER", "bull_call_spread", median="-0.05", best="-0.01",
                       positive_fraction="0"),
            _candidate("ALSO", "bear_put_spread", median="-0.12", best="-0.02",
                       positive_fraction="0"),
        ]

        decisions = decide_all_configurations(
            universe, V4_CONFIGURATIONS, evidence=NO_MOVE_EVIDENCE
        )

        assert [d.status for d in decisions] == [STATUS_NO_ACTION] * 6
        for decision in decisions:
            assert decision.selected_candidate_id is None
            assert decision.diagnostics.economic_rejected_count == 2
            assert "ECONOMIC_VIABILITY" in (decision.no_action_reason or "")

    def test_a_candidate_that_cannot_be_valued_is_never_ranked(self):
        """Section 23 -- a data-quality refusal is not an economics one."""
        universe = [
            _candidate("BAD", "bull_call_spread", median="0.30",
                       data_invalid_reason="no entry IV on leg(s) [1]"),
        ]

        for decision in decide_all_configurations(
            universe, V4_CONFIGURATIONS, evidence=NO_MOVE_EVIDENCE
        ):
            assert decision.status == STATUS_NO_ACTION
            assert decision.diagnostics.data_invalid_count == 1
            assert decision.diagnostics.economic_rejected_count == 0


class TestOneConfigurationDeclines:
    """Section 80."""

    def test_conservative_declines_a_family_aggressive_takes(self):
        """A single-leg long is excluded for Conservative by the risk-profile
        rule this project already owns -- not by a new number introduced
        here."""
        universe = [
            _candidate("LONGCALL", "long_call", median="0.35", entry_cash="250",
                       max_risk="250", n_legs=1),
        ]

        decided = _by_key(
            decide_all_configurations(universe, V4_CONFIGURATIONS, evidence=NO_MOVE_EVIDENCE)
        )

        for key in ("v4_2k_conservative", "v4_10k_conservative"):
            assert decided[key].status == STATUS_NO_ACTION
            assert decided[key].diagnostics.strategy_not_permitted_count == 1
            assert STAGE_FAMILY in (decided[key].no_action_reason or "")

        for key in ("v4_2k_moderate", "v4_2k_aggressive", "v4_10k_moderate",
                    "v4_10k_aggressive"):
            assert decided[key].status == STATUS_ACTION
            assert decided[key].selected_candidate_id == "LONGCALL"

    def test_a_thin_two_sided_market_is_refused_by_the_profiles_with_a_floor(self):
        """One of four legs quoted two-sided is 0.25 coverage: below
        Conservative's 0.80 floor and below Moderate's 0.40, so both decline
        it. Aggressive has no floor beyond the actionability gate every
        profile already faces upstream, so it may still take it."""
        universe = [
            _candidate("THIN", "bull_call_spread", median="0.30", entry_cash="250",
                       max_risk="250", n_legs=4, two_sided_legs=1),
        ]

        decided = _by_key(
            decide_all_configurations(universe, V4_CONFIGURATIONS, evidence=NO_MOVE_EVIDENCE)
        )

        assert decided["v4_2k_conservative"].diagnostics.liquidity_rejected_count == 1
        assert decided["v4_2k_moderate"].diagnostics.liquidity_rejected_count == 1
        assert decided["v4_2k_aggressive"].diagnostics.liquidity_rejected_count == 0
        assert decided["v4_2k_aggressive"].status == STATUS_ACTION

    def test_capital_refusal_names_the_configuration_that_could_not_hold_it(self):
        universe = [
            _candidate("BIG", "bull_call_spread", median="0.30", entry_cash="6000",
                       max_risk="6000"),
        ]

        decided = _by_key(
            decide_all_configurations(universe, V4_CONFIGURATIONS, evidence=NO_MOVE_EVIDENCE)
        )

        two_k = decided["v4_2k_aggressive"]
        assert two_k.status == STATUS_NO_ACTION
        assert two_k.diagnostics.capital_rejected_count == 1
        assert STAGE_CAPITAL in (two_k.no_action_reason or "")
        # $10K Aggressive's cap is $5,000, so this is a RISK refusal there --
        # a different constraint, correctly named.
        ten_k = decided["v4_10k_aggressive"]
        assert ten_k.diagnostics.capital_rejected_count == 0
        assert ten_k.diagnostics.risk_rejected_count == 1


class TestNoNakedShorts:
    """Section 19."""

    @pytest.mark.parametrize("category", list(StrategyCategory))
    def test_every_registry_family_is_bounded_risk(self, category):
        """Phase 2 draws its families from the canonical registry, so the
        guarantee is that the registry itself contains no uncovered short.
        If a naked family is ever added, this fails and the sizing rule that
        refuses unbounded structures has to be revisited deliberately."""
        try:
            semantics = get_strategy_semantics(category)
        except (KeyError, ValueError):
            pytest.skip(f"{category.value} has no semantics entry")
        assert "uncovered" not in semantics.payoff_shape
        assert "naked" not in semantics.payoff_shape

    def test_a_structure_without_a_bounded_loss_is_never_sized(self):
        universe = [
            SharedCandidate(
                candidate_id="UNBOUNDED",
                strategy="long_call",
                economics=CandidateEconomics(
                    candidate_id="UNBOUNDED",
                    strategy="long_call",
                    median_return=Decimal("0.50"),
                    worst_return=Decimal("-0.10"),
                    best_return=Decimal("0.90"),
                    positive_scenario_fraction=Decimal("0.70"),
                    no_profitable_region=False,
                    semantic_compatibility=Decimal("0.90"),
                    mean_relative_spread=Decimal("0.04"),
                ),
                entry_cash_required=Decimal("100"),
                per_contract_max_risk=None,
                n_legs=1,
                n_legs_with_two_sided_quote=1,
            )
        ]

        for decision in decide_all_configurations(
            universe, V4_CONFIGURATIONS, evidence=NO_MOVE_EVIDENCE
        ):
            assert decision.status == STATUS_NO_ACTION
            assert decision.position is None
