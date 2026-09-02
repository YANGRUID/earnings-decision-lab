from datetime import date
from decimal import Decimal

import pytest

from analytics.decision.budget import (
    CONTRACT_MULTIPLIER,
    compute_budget_fit,
    filter_and_size_by_budget,
    usable_risk_budget,
    validate_risk_cap_inputs,
)
from analytics.decision.strategy_scoring import ViewRankedStrategy
from analytics.options.payoff import Action, OptionLeg, analyze
from analytics.options.strategy_candidates import StrategyCandidate, StrategyCategory
from models.enums import OptionType

EXP = date(2026, 9, 18)
UNDERLYING = Decimal("100")


def _candidate(legs: list[OptionLeg], category: StrategyCategory = StrategyCategory.LONG_CALL):
    return StrategyCandidate(
        category=category,
        legs=tuple(legs),
        analysis=analyze(legs),
        expiration=EXP,
        underlying_price=UNDERLYING,
    )


def _debit_candidate(premium: Decimal) -> StrategyCandidate:
    legs = [OptionLeg(OptionType.CALL, Action.BUY, Decimal("100"), premium)]
    return _candidate(legs)


def _credit_spread_candidate() -> StrategyCandidate:
    legs = [
        OptionLeg(OptionType.PUT, Action.SELL, Decimal("95"), Decimal("3")),
        OptionLeg(OptionType.PUT, Action.BUY, Decimal("90"), Decimal("1")),
    ]
    return _candidate(legs, category=StrategyCategory.PUT_CREDIT_SPREAD)


class TestUsableRiskBudget:
    def test_no_risk_cap_uses_the_full_budget(self):
        assert usable_risk_budget(Decimal("5000"), None, False) == Decimal("5000")

    def test_dollar_risk_cap_caps_below_the_budget(self):
        assert usable_risk_budget(Decimal("5000"), Decimal("1000"), False) == Decimal("1000")

    def test_dollar_risk_cap_never_exceeds_the_budget(self):
        # A risk cap larger than the budget itself can't unlock more than
        # the budget actually has.
        assert usable_risk_budget(Decimal("500"), Decimal("10000"), False) == Decimal("500")

    def test_percent_risk_cap_computes_a_fraction_of_budget(self):
        assert usable_risk_budget(Decimal("10000"), Decimal("25"), True) == Decimal("2500")

    def test_percent_risk_cap_above_100_never_exceeds_the_budget(self):
        # Regression test for the real P0 bug this was built to fix: a
        # risk_cap of 5000 submitted with risk_cap_is_percent=True (a
        # dollar figure typed into a "% of budget" field left on its
        # default unit) previously multiplied straight through with no
        # ceiling -- trade_budget * (5000/100) = 50x the real budget --
        # sizing a real NVDA butterfly to a $500,000 max loss on a
        # $10,000 budget (5000% utilization, -$490,000 remaining). A risk
        # cap narrows the budget; it can never widen it, regardless of
        # what value or unit it's given in.
        assert usable_risk_budget(Decimal("10000"), Decimal("5000"), True) == Decimal("10000")

    def test_negative_risk_cap_yields_zero_usable_budget(self):
        assert usable_risk_budget(Decimal("10000"), Decimal("-5"), False) == Decimal(0)


class TestValidateRiskCapInputs:
    def test_no_risk_cap_is_always_valid(self):
        validate_risk_cap_inputs(None, False)  # must not raise

    def test_percent_risk_cap_within_range_is_valid(self):
        validate_risk_cap_inputs(Decimal("25"), True)  # must not raise
        validate_risk_cap_inputs(Decimal("100"), True)  # must not raise

    def test_dollar_risk_cap_above_100_is_valid(self):
        # 100 is only a ceiling for *percent* risk caps -- a $5,000 dollar
        # risk cap on a $10,000 budget is completely normal.
        validate_risk_cap_inputs(Decimal("5000"), False)  # must not raise

    def test_percent_risk_cap_above_100_is_rejected(self):
        # The exact real-world input that produced the P0 bug: rejected
        # outright now, rather than silently clamped.
        with pytest.raises(ValueError, match=r"risk_cap_is_percent=true"):
            validate_risk_cap_inputs(Decimal("5000"), True)

    def test_negative_risk_cap_is_rejected(self):
        with pytest.raises(ValueError, match="cannot be negative"):
            validate_risk_cap_inputs(Decimal("-10"), False)


class TestComputeBudgetFit:
    def test_500_budget_affords_one_contract_of_a_200_dollar_max_loss_debit(self):
        # A long call costing $2.00/share = $200 max loss per contract.
        candidate = _debit_candidate(Decimal("2"))
        fit = compute_budget_fit(candidate, trade_budget=Decimal("500"))
        assert fit.feasible is True
        assert fit.capital_at_risk_per_contract == Decimal("200")
        assert fit.max_feasible_quantity == 2  # floor(500/200) = 2
        assert fit.total_max_loss == Decimal("400")
        assert fit.remaining_budget == Decimal("100")

    def test_10000_budget_affords_more_contracts_of_the_same_structure(self):
        candidate = _debit_candidate(Decimal("2"))
        fit = compute_budget_fit(candidate, trade_budget=Decimal("10000"))
        assert fit.max_feasible_quantity == 50  # floor(10000/200)
        assert fit.total_max_loss == Decimal("10000")

    def test_500_budget_cannot_afford_even_one_contract_of_an_expensive_debit(self):
        # A $7.20/share long call costs $720/contract -- unaffordable at $500.
        candidate = _debit_candidate(Decimal("7.20"))
        fit = compute_budget_fit(candidate, trade_budget=Decimal("500"))
        assert fit.feasible is False
        assert fit.max_feasible_quantity == 0
        assert fit.minimum_required == Decimal("720")
        assert fit.total_max_loss is None

    def test_credit_spread_sizes_by_its_real_max_loss_not_premium_received(self):
        # A $95/$90 put credit spread here has max_loss = width(5) - credit(2)
        # = $3.00/share = $300/contract -- sizing must use max_loss, never
        # the (smaller, misleading) net credit received.
        candidate = _credit_spread_candidate()
        assert candidate.analysis.net_premium < 0  # confirms it's a real credit
        fit = compute_budget_fit(candidate, trade_budget=Decimal("900"))
        assert fit.capital_at_risk_per_contract == Decimal("300")
        assert fit.max_feasible_quantity == 3

    def test_risk_cap_restricts_usable_budget_below_the_stated_trade_budget(self):
        candidate = _debit_candidate(Decimal("2"))  # $200/contract
        fit = compute_budget_fit(
            candidate,
            trade_budget=Decimal("10000"),
            risk_cap=Decimal("25"),
            risk_cap_is_percent=True,
        )
        # 25% of 10000 = 2500 usable -> floor(2500/200) = 12 contracts, not 50.
        assert fit.usable_risk_budget == Decimal("2500")
        assert fit.max_feasible_quantity == 12

    def test_zero_budget_is_never_feasible(self):
        candidate = _debit_candidate(Decimal("2"))
        fit = compute_budget_fit(candidate, trade_budget=Decimal("0"))
        assert fit.feasible is False
        assert fit.max_feasible_quantity == 0

    def test_total_net_premium_scales_with_feasible_quantity(self):
        candidate = _debit_candidate(Decimal("2"))
        fit = compute_budget_fit(candidate, trade_budget=Decimal("500"))
        # quantity=2, premium=2/share * 100 multiplier * 2 contracts = 400
        assert fit.total_net_premium == Decimal("400")


def _ranked(rank: int, candidate: StrategyCandidate) -> ViewRankedStrategy:
    return ViewRankedStrategy(
        candidate=candidate,
        rank=rank,
        target_price=None,
        payoff_at_target=None,
        score=None,  # type: ignore[arg-type] -- unused by filter_and_size_by_budget
        move_compatibility=None,
    )


class TestFilterAndSizeByBudget:
    def test_no_budget_is_a_pure_no_op(self):
        ranked = [_ranked(1, _debit_candidate(Decimal("2")))]
        feasible, fits, minimum = filter_and_size_by_budget(ranked, trade_budget=None)
        assert feasible == ranked
        assert fits == {}
        assert minimum is None

    def test_500_budget_excludes_the_expensive_structure_but_keeps_the_cheap_one(self):
        cheap = _ranked(1, _debit_candidate(Decimal("2")))  # $200/contract
        expensive = _ranked(2, _debit_candidate(Decimal("7.20")))  # $720/contract
        feasible, fits, minimum = filter_and_size_by_budget(
            [cheap, expensive], trade_budget=Decimal("500")
        )
        assert feasible == [cheap]
        assert fits[1].feasible is True
        assert fits[2].feasible is False
        assert minimum is None  # at least one candidate WAS affordable

    def test_10000_budget_keeps_both_structures_the_500_budget_could_not_afford(self):
        cheap = _ranked(1, _debit_candidate(Decimal("2")))
        expensive = _ranked(2, _debit_candidate(Decimal("7.20")))
        feasible, fits, minimum = filter_and_size_by_budget(
            [cheap, expensive], trade_budget=Decimal("10000")
        )
        assert feasible == [cheap, expensive]
        assert minimum is None

    def test_nothing_affordable_reports_the_real_minimum_required(self):
        expensive = _ranked(1, _debit_candidate(Decimal("7.20")))  # $720/contract
        even_more_expensive = _ranked(2, _debit_candidate(Decimal("12")))  # $1200/contract
        feasible, fits, minimum = filter_and_size_by_budget(
            [expensive, even_more_expensive], trade_budget=Decimal("100")
        )
        assert feasible == []
        assert minimum == Decimal("720")  # the cheapest of the two real structures

    def test_ranking_order_is_preserved_among_the_feasible_ones(self):
        # Best-fit-for-view is rank 1; both affordable -- order must survive
        # budget filtering unchanged (filtering never re-ranks).
        first = _ranked(1, _debit_candidate(Decimal("2")))
        second = _ranked(2, _debit_candidate(Decimal("3")))
        feasible, _, _ = filter_and_size_by_budget([first, second], trade_budget=Decimal("10000"))
        assert [r.rank for r in feasible] == [1, 2]


def _multi_leg_candidates() -> dict[str, StrategyCandidate]:
    """One real candidate per Part 35 strategy type, all built from the
    same leg-builder functions the real engine uses (analytics/options/
    strategies.py) -- never hand-rolled legs that could mask a real
    builder bug."""
    from analytics.options import strategies as s

    return {
        # bull_call_spread(long_strike, long_premium, short_strike, short_premium)
        "bull_call_spread": _candidate(
            s.bull_call_spread(Decimal("100"), Decimal("3.5"), Decimal("103"), Decimal("1.8")),
            StrategyCategory.BULL_CALL_SPREAD,
        ),
        # bear_put_spread(long_strike, long_premium, short_strike, short_premium)
        "bear_put_spread": _candidate(
            s.bear_put_spread(Decimal("100"), Decimal("3.2"), Decimal("97"), Decimal("1.6")),
            StrategyCategory.BEAR_PUT_SPREAD,
        ),
        # long_call_butterfly(lower_strike, lower_premium, middle_strike,
        # middle_premium, upper_strike, upper_premium)
        "long_call_butterfly": _candidate(
            s.long_call_butterfly(
                Decimal("95"),
                Decimal("6.5"),
                Decimal("100"),
                Decimal("2.8"),
                Decimal("105"),
                Decimal("0.6"),
            ),
            StrategyCategory.LONG_CALL_BUTTERFLY,
        ),
        # iron_butterfly(put_long_strike, put_long_premium, center_strike,
        # put_short_premium, call_short_premium, call_long_strike, call_long_premium)
        "iron_butterfly": _candidate(
            s.iron_butterfly(
                Decimal("95"),
                Decimal("1.0"),
                Decimal("100"),
                Decimal("2.5"),
                Decimal("2.6"),
                Decimal("105"),
                Decimal("1.1"),
            ),
            StrategyCategory.IRON_BUTTERFLY,
        ),
        # iron_condor(put_long_strike, put_long_premium, put_short_strike,
        # put_short_premium, call_short_strike, call_short_premium,
        # call_long_strike, call_long_premium)
        "iron_condor": _candidate(
            s.iron_condor(
                Decimal("90"),
                Decimal("0.8"),
                Decimal("95"),
                Decimal("1.5"),
                Decimal("105"),
                Decimal("1.4"),
                Decimal("110"),
                Decimal("0.7"),
            ),
            StrategyCategory.IRON_CONDOR,
        ),
        # long_straddle(strike, call_premium, put_premium)
        "long_straddle": _candidate(
            s.long_straddle(Decimal("100"), Decimal("4.2"), Decimal("3.9")),
            StrategyCategory.LONG_STRADDLE,
        ),
        # long_strangle(put_strike, put_premium, call_strike, call_premium)
        "long_strangle": _candidate(
            s.long_strangle(Decimal("95"), Decimal("2.1"), Decimal("105"), Decimal("1.9")),
            StrategyCategory.LONG_STRANGLE,
        ),
    }


class TestLegQuantitiesAndMultiplier:
    """Part 35: every multi-leg strategy's real leg quantities, and the
    contract multiplier applied exactly once between per-share and
    per-contract dollar amounts."""

    def test_long_call_butterfly_has_1_2_1_leg_quantities(self):
        candidate = _multi_leg_candidates()["long_call_butterfly"]
        quantities = [leg.quantity for leg in candidate.legs]
        assert quantities == [1, 2, 1]

    def test_iron_butterfly_has_four_single_quantity_legs(self):
        candidate = _multi_leg_candidates()["iron_butterfly"]
        assert [leg.quantity for leg in candidate.legs] == [1, 1, 1, 1]

    def test_iron_condor_has_four_single_quantity_legs(self):
        candidate = _multi_leg_candidates()["iron_condor"]
        assert [leg.quantity for leg in candidate.legs] == [1, 1, 1, 1]

    @pytest.mark.parametrize("name", list(_multi_leg_candidates().keys()))
    def test_capital_at_risk_per_contract_applies_the_multiplier_exactly_once(self, name):
        candidate = _multi_leg_candidates()[name]
        fit = compute_budget_fit(candidate, trade_budget=Decimal("1000000"))
        if candidate.analysis.max_loss is None:
            pytest.skip(f"{name} has unbounded max loss")
        assert fit.capital_at_risk_per_contract == candidate.analysis.max_loss * CONTRACT_MULTIPLIER


class TestBudgetInvariants:
    """Part 10's hard invariant, swept across every real strategy shape
    and a range of budgets/risk caps: total_max_loss must never exceed
    usable_risk_budget, utilization must stay within [0, 100], and
    remaining_budget must never go negative. This is the permanent
    regression coverage for the real P0 bug (a $10,000 budget producing
    a $500,000 max loss via a mis-unit'd percent risk cap)."""

    @pytest.mark.parametrize("name", list(_multi_leg_candidates().keys()))
    @pytest.mark.parametrize("budget", [Decimal("500"), Decimal("2500"), Decimal("10000")])
    def test_max_loss_never_exceeds_budget(self, name, budget):
        candidate = _multi_leg_candidates()[name]
        fit = compute_budget_fit(candidate, trade_budget=budget)
        if not fit.feasible:
            return
        assert fit.total_max_loss <= budget
        assert Decimal(0) <= fit.budget_utilization_pct <= Decimal(100)
        assert fit.remaining_budget >= Decimal(0)

    @pytest.mark.parametrize("name", list(_multi_leg_candidates().keys()))
    @pytest.mark.parametrize("risk_cap_pct", [Decimal("1"), Decimal("25"), Decimal("100")])
    def test_max_loss_never_exceeds_budget_with_a_percent_risk_cap(self, name, risk_cap_pct):
        candidate = _multi_leg_candidates()[name]
        budget = Decimal("10000")
        fit = compute_budget_fit(
            candidate, trade_budget=budget, risk_cap=risk_cap_pct, risk_cap_is_percent=True
        )
        if not fit.feasible:
            return
        assert fit.total_max_loss <= budget
        assert fit.total_max_loss <= fit.usable_risk_budget
        assert Decimal(0) <= fit.budget_utilization_pct <= Decimal(100)
        assert fit.remaining_budget >= Decimal(0)

    def test_the_exact_reported_p0_case_no_longer_reproduces(self):
        # The real NVDA long call butterfly from the live bug report:
        # legs [buy 1 @222.50/$8.70, sell 2 @225.00/$7.43, buy 1 @227.50/
        # $6.25] -> net_premium/max_loss = $0.10/share. $10,000 budget,
        # risk_cap=5000 submitted as risk_cap_is_percent=True.
        from analytics.options import strategies as s

        candidate = _candidate(
            s.long_call_butterfly(
                Decimal("222.50"),
                Decimal("8.70"),
                Decimal("225.00"),
                Decimal("7.425"),
                Decimal("227.50"),
                Decimal("6.25"),
            ),
            StrategyCategory.LONG_CALL_BUTTERFLY,
        )
        assert candidate.analysis.max_loss == Decimal("0.10")

        fit = compute_budget_fit(
            candidate,
            trade_budget=Decimal("10000"),
            risk_cap=Decimal("5000"),
            risk_cap_is_percent=True,
        )
        # Previously: max_feasible_quantity=50000, total_max_loss=500000,
        # budget_utilization_pct=5000, remaining_budget=-490000.
        assert fit.max_feasible_quantity == 1000
        assert fit.total_max_loss == Decimal("10000")
        assert fit.budget_utilization_pct == Decimal("100")
        assert fit.remaining_budget == Decimal("0")
