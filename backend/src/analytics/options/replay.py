"""Historical event replay: deterministic strike selection + strategy
reconstruction, built on the Phase 3 payoff engine.

Strike selection is rule-based and must never be chosen with knowledge of
an event's actual outcome — see StrikeSelectionRule. This module has no
knowledge of "what happened later"; callers pass in the strikes that were
actually available at entry time and, separately, an evaluation price if
they want to see how the position played out.

No historical options-chain provider is currently wired up (see
docs/data_sources.md), so this engine has real inputs to run against
exactly nowhere yet — it's built and unit-tested against clearly-labeled
synthetic strike/quote data, ready to run for real the moment a provider is
selected. See docs/earnings_methodology.md.
"""

import enum
from dataclasses import dataclass
from decimal import Decimal

from analytics.options.payoff import OptionLeg, analyze, total_payoff


class StrikeSelectionRule(enum.StrEnum):
    NEAREST_ATM = "nearest_atm"
    FIXED_PCT_OTM = "fixed_pct_otm"
    NEAREST_TO_TARGET = "nearest_to_target"


def nearest_available_strike(available_strikes: set[Decimal], target: Decimal) -> Decimal:
    if not available_strikes:
        raise ValueError("no strikes available")
    return min(available_strikes, key=lambda k: abs(k - target))


def select_strike(
    rule: StrikeSelectionRule,
    available_strikes: set[Decimal],
    underlying_price: Decimal,
    *,
    otm_pct: Decimal | None = None,
    direction: str | None = None,
    target: Decimal | None = None,
) -> Decimal:
    """A single, deterministic entry point for every supported rule — keeps
    "how was this strike chosen" auditable to one function regardless of
    which rule a replay used.
    """
    if rule == StrikeSelectionRule.NEAREST_ATM:
        return nearest_available_strike(available_strikes, underlying_price)

    if rule == StrikeSelectionRule.FIXED_PCT_OTM:
        if otm_pct is None or direction not in ("up", "down"):
            raise ValueError("fixed_pct_otm requires otm_pct and direction='up'|'down'")
        multiplier = (1 + otm_pct) if direction == "up" else (1 - otm_pct)
        return nearest_available_strike(available_strikes, underlying_price * multiplier)

    if rule == StrikeSelectionRule.NEAREST_TO_TARGET:
        if target is None:
            raise ValueError("nearest_to_target requires target")
        return nearest_available_strike(available_strikes, target)

    raise ValueError(f"unknown strike selection rule: {rule}")


@dataclass(frozen=True)
class ReplayResult:
    strategy_name: str
    strike_selection_rule: StrikeSelectionRule
    legs: tuple[OptionLeg, ...]
    net_premium: Decimal
    underlying_price_at_entry: Decimal
    max_profit: Decimal | None
    max_loss: Decimal | None
    breakevens: tuple[Decimal, ...]
    underlying_price_at_evaluation: Decimal | None = None
    payoff_at_evaluation: Decimal | None = None


def build_replay(
    strategy_name: str,
    strike_selection_rule: StrikeSelectionRule,
    legs: list[OptionLeg],
    underlying_price_at_entry: Decimal,
    underlying_price_at_evaluation: Decimal | None = None,
) -> ReplayResult:
    """Reconstruct entry economics for ``legs`` (already chosen via
    ``select_strike`` and priced from real quotes) and, if an evaluation
    price is given (e.g. the actual post-earnings close), the resulting
    payoff — never a strike or evaluation price chosen after the fact to
    flatter the result.
    """
    analysis = analyze(legs)
    payoff_at_evaluation = (
        total_payoff(legs, underlying_price_at_evaluation)
        if underlying_price_at_evaluation is not None
        else None
    )
    return ReplayResult(
        strategy_name=strategy_name,
        strike_selection_rule=strike_selection_rule,
        legs=tuple(legs),
        net_premium=analysis.net_premium,
        underlying_price_at_entry=underlying_price_at_entry,
        max_profit=analysis.max_profit,
        max_loss=analysis.max_loss,
        breakevens=analysis.breakevens,
        underlying_price_at_evaluation=underlying_price_at_evaluation,
        payoff_at_evaluation=payoff_at_evaluation,
    )
