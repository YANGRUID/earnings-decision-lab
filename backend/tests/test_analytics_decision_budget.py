from datetime import date
from decimal import Decimal

from analytics.decision.budget import (
    compute_budget_fit,
    filter_and_size_by_budget,
    usable_risk_budget,
)
from analytics.decision.strategy_scoring import ViewRankedStrategy
from analytics.options.payoff import Action, OptionLeg, analyze
from analytics.options.strategy_candidates import StrategyCandidate, StrategyCategory

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
    from models.enums import OptionType

    legs = [OptionLeg(OptionType.CALL, Action.BUY, Decimal("100"), premium)]
    return _candidate(legs)


def _credit_spread_candidate() -> StrategyCandidate:
    from models.enums import OptionType

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
        feasible, _, _ = filter_and_size_by_budget(
            [first, second], trade_budget=Decimal("10000")
        )
        assert [r.rank for r in feasible] == [1, 2]
