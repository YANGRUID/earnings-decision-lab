"""V3 sizing refusals name the BINDING constraint (V4 consolidation,
Section 15). Methodology untouched; wording only, prospectively."""

from decimal import Decimal
from types import SimpleNamespace

from analytics.decision.budget import BudgetFit
from models.enums import RiskProfile
from services.benchmark_entry_capture import _describe_sizing_refusal


def _fit(per_contract, budget="2000", usable="600", cap="30"):
    return BudgetFit(
        trade_budget=Decimal(budget), risk_cap=Decimal(cap),
        usable_risk_budget=Decimal(usable),
        capital_at_risk_per_contract=Decimal(per_contract) if per_contract else None,
        max_feasible_quantity=0, total_max_loss=None, total_max_profit=None,
        total_net_premium=None, budget_utilization_pct=None, remaining_budget=None,
        feasible=False, minimum_required=None,
    )


def _portfolio(profile=RiskProfile.MODERATE):
    return SimpleNamespace(risk_profile=profile, cash_balance=Decimal("2000"))


class TestBindingConstraintIsNamed:
    def test_the_real_panw_case_is_a_risk_cap_refusal_not_a_budget_one(self):
        """2026-09-01: $1,155 fits the $2,000 budget; the 30% cap ($600) is
        what bound. The old text blamed the budget."""
        text = _describe_sizing_refusal(_fit("1155"), _portfolio())
        assert text.startswith("Risk cap exceeded")
        assert "$1,155.00" in text
        assert "$600.00" in text
        assert "30% of $2,000" in text
        assert "cannot size even one contract" not in text

    def test_a_structure_larger_than_the_account_is_capital_insufficient(self):
        text = _describe_sizing_refusal(_fit("2500"), _portfolio())
        assert text.startswith("Capital insufficient")
        assert "$2,500.00" in text and "$2,000" in text

    def test_the_two_refusals_are_distinguishable(self):
        risk = _describe_sizing_refusal(_fit("1155"), _portfolio())
        capital = _describe_sizing_refusal(_fit("2500"), _portfolio())
        assert risk.split(":")[0] != capital.split(":")[0]

    def test_unpriceable_structure_says_so_rather_than_blaming_the_budget(self):
        text = _describe_sizing_refusal(_fit(None), _portfolio())
        assert "no bounded, priceable maximum loss" in text

    def test_profile_name_is_the_portfolios_own(self):
        text = _describe_sizing_refusal(
            _fit("1155", usable="300", cap="15"), _portfolio(RiskProfile.CONSERVATIVE)
        )
        assert "Conservative permits $300.00" in text
