"""Budget-aware feasibility and sizing for the AI Options Decision Engine
and Strategy Lab (Phase 14.10 Part G). Fully deterministic -- no LLM
involvement, no invented broker margin requirements. Given a real
StrategyCandidate (whose max_loss/max_profit/net_premium are already
computed by analytics/options/payoff.py) and an owner-supplied trade
budget (+ optional risk cap), this answers: how many contracts of this
exact structure actually fit, and what does that cost.

Budget's real, honest influence on this project's recommendations is
feasibility and sizing, not a fabricated "better budget = better
strategy" score: a $500 budget and a $10,000 budget looking at the same
real chain will size the same affordable structure differently, and will
disagree on which structures are even possible at all -- that is exactly
what changes the *recommended* candidate between budget tiers (see
services/decision_engine.py, which filters to budget-feasible candidates
before ranking).

capital_at_risk_per_contract is always the real, already-computed
max_loss for one contract (multiplied by the standard 100-share options
contract multiplier) -- this project generates no undefined-risk
category today (see analytics/options/strategy_candidates.py), but if
max_loss is ever None (unbounded), budget fit is honestly reported as
unavailable rather than guessed.
"""

from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal
from typing import TYPE_CHECKING

from analytics.options.strategy_candidates import StrategyCandidate

if TYPE_CHECKING:
    from analytics.decision.strategy_scoring import ViewRankedStrategy

CONTRACT_MULTIPLIER = Decimal(100)


@dataclass(frozen=True)
class BudgetFit:
    trade_budget: Decimal
    risk_cap: Decimal | None
    usable_risk_budget: Decimal
    capital_at_risk_per_contract: Decimal | None
    max_feasible_quantity: int
    total_max_loss: Decimal | None
    total_max_profit: Decimal | None
    total_net_premium: Decimal | None
    budget_utilization_pct: Decimal | None
    remaining_budget: Decimal | None
    feasible: bool
    minimum_required: Decimal | None


def usable_risk_budget(
    trade_budget: Decimal, risk_cap: Decimal | None, risk_cap_is_percent: bool
) -> Decimal:
    """The real dollar amount actually at risk given both inputs -- an
    owner may set a budget (total capital they'd deploy) and, separately,
    a risk cap (how much of it they're willing to actually lose). When no
    risk cap is set, the whole budget is usable risk capital (a debit
    strategy's cost basis already caps loss at what you paid)."""
    if risk_cap is None:
        return trade_budget
    if risk_cap_is_percent:
        return trade_budget * (risk_cap / Decimal(100))
    return min(risk_cap, trade_budget)


def compute_budget_fit(
    candidate: StrategyCandidate,
    *,
    trade_budget: Decimal,
    risk_cap: Decimal | None = None,
    risk_cap_is_percent: bool = False,
) -> BudgetFit:
    usable = usable_risk_budget(trade_budget, risk_cap, risk_cap_is_percent)
    max_loss = candidate.analysis.max_loss
    per_contract = max_loss * CONTRACT_MULTIPLIER if max_loss is not None else None

    if per_contract is None or per_contract <= 0 or usable <= 0:
        # Undefined risk (never generated today), a free/zero-cost
        # structure, or literally no usable budget -- quantity sizing
        # doesn't honestly apply; report unavailable, never guessed.
        return BudgetFit(
            trade_budget=trade_budget,
            risk_cap=risk_cap,
            usable_risk_budget=usable,
            capital_at_risk_per_contract=per_contract,
            max_feasible_quantity=0,
            total_max_loss=None,
            total_max_profit=None,
            total_net_premium=None,
            budget_utilization_pct=None,
            remaining_budget=None,
            feasible=False,
            minimum_required=per_contract,
        )

    quantity = int((usable / per_contract).to_integral_value(rounding=ROUND_FLOOR))
    if quantity < 1:
        return BudgetFit(
            trade_budget=trade_budget,
            risk_cap=risk_cap,
            usable_risk_budget=usable,
            capital_at_risk_per_contract=per_contract,
            max_feasible_quantity=0,
            total_max_loss=None,
            total_max_profit=None,
            total_net_premium=None,
            budget_utilization_pct=None,
            remaining_budget=None,
            feasible=False,
            minimum_required=per_contract,
        )

    total_max_loss = per_contract * quantity
    total_max_profit = (
        candidate.analysis.max_profit * CONTRACT_MULTIPLIER * quantity
        if candidate.analysis.max_profit is not None
        else None
    )
    total_net_premium = candidate.analysis.net_premium * CONTRACT_MULTIPLIER * quantity
    utilization = (total_max_loss / trade_budget * 100) if trade_budget > 0 else None
    remaining = trade_budget - total_max_loss

    return BudgetFit(
        trade_budget=trade_budget,
        risk_cap=risk_cap,
        usable_risk_budget=usable,
        capital_at_risk_per_contract=per_contract,
        max_feasible_quantity=quantity,
        total_max_loss=total_max_loss,
        total_max_profit=total_max_profit,
        total_net_premium=total_net_premium,
        budget_utilization_pct=utilization,
        remaining_budget=remaining,
        feasible=True,
        minimum_required=None,
    )


def filter_and_size_by_budget(
    ranked: list["ViewRankedStrategy"],
    *,
    trade_budget: Decimal | None,
    risk_cap: Decimal | None = None,
    risk_cap_is_percent: bool = False,
) -> tuple[list["ViewRankedStrategy"], dict[int, BudgetFit], Decimal | None]:
    """The real mechanism by which a $500 and a $10,000 budget can
    recommend genuinely different structures (Phase 14.10 Part G1) --
    restricts an already view-ranked candidate list to the ones the
    stated budget can actually afford, preserving their relative order.

    Returns ``(feasible_ranked, budget_fits_by_rank, infeasible_minimum)``:
    ``budget_fits_by_rank`` is keyed by each ViewRankedStrategy's own
    ``.rank`` (stable and unique within one ranking result); when no
    candidate at all is affordable, ``feasible_ranked`` is empty and
    ``infeasible_minimum`` reports the cheapest real structure this chain
    actually supports, so the caller can tell the owner what budget would
    be required instead of just returning nothing.

    ``trade_budget=None`` (no budget stated) is a pure no-op: the input
    list is returned unchanged with an empty fits map, matching every
    caller's pre-budget behavior exactly.
    """
    if trade_budget is None:
        return ranked, {}, None

    fits = {
        r.rank: compute_budget_fit(
            r.candidate,
            trade_budget=trade_budget,
            risk_cap=risk_cap,
            risk_cap_is_percent=risk_cap_is_percent,
        )
        for r in ranked
    }
    feasible = [r for r in ranked if fits[r.rank].feasible]

    infeasible_minimum: Decimal | None = None
    if not feasible and ranked:
        candidate_minimums = [
            bf.minimum_required for bf in fits.values() if bf.minimum_required is not None
        ]
        infeasible_minimum = min(candidate_minimums) if candidate_minimums else None

    return feasible, fits, infeasible_minimum
