"""Deterministic, explainable ranking of strategy candidates (Phase 14).

Every ranking signal here is derived from real numbers already computed
by analytics/options/payoff.py and (when available) the company's real,
most recently computed implied move (VolatilitySnapshot) -- never an LLM
judgment, and never a fabricated probability or "confidence score". The
LLM-facing Earnings Thesis (services/earnings_thesis.py) may describe
this ranking in prose, but the ranking itself -- and the ``explanation``
string attached to each result -- is built entirely from these numbers,
never invented.

Primary methodology, when a real implied move is on record: evaluate each
candidate's real payoff (analytics/options/payoff.py's total_payoff) at
the underlying price moving down and up by exactly the market's own
implied-move percentage -- the same number this project already surfaces
throughout the app (VolatilitySnapshot.implied_move_pct) -- then score by
the average of those two P&L outcomes relative to the strategy's own real
maximum loss. This is a standard, defensible risk-adjusted heuristic, not
presented as the only correct one (see docs/options_methodology.md's own
precedent of naming alternatives it didn't implement): it rewards
strategies that pay off well if the market's own pricing is roughly
right, scaled by how much capital they put at risk, but does not model
the *probability* of any particular move (that would require assuming a
price distribution -- see analytics/options/black_scholes.py's
documented assumptions -- which this project does not yet apply here).

Fallback, when no implied move is available yet for this company's
upcoming earnings: rank by real maximum loss alone, smallest first --
capital-at-risk is always computable for every candidate this project
generates (every required strategy category has a bounded max loss by
construction; see analytics/options/strategy_candidates.py), even when
nothing else is.
"""

from dataclasses import dataclass
from decimal import Decimal

from analytics.options.payoff import total_payoff
from analytics.options.strategy_candidates import StrategyCandidate


@dataclass(frozen=True)
class ScenarioPnl:
    down_price: Decimal
    down_pnl: Decimal
    flat_pnl: Decimal
    up_price: Decimal
    up_pnl: Decimal


@dataclass(frozen=True)
class RankedStrategy:
    candidate: StrategyCandidate
    rank: int  # 1-indexed, 1 = best by this methodology
    scenario: ScenarioPnl | None  # None only in the no-implied-move fallback
    score: Decimal
    explanation: str


def _scenario_pnl(candidate: StrategyCandidate, implied_move_pct: Decimal) -> ScenarioPnl:
    legs = list(candidate.legs)
    down_price = candidate.underlying_price * (1 - implied_move_pct)
    up_price = candidate.underlying_price * (1 + implied_move_pct)
    return ScenarioPnl(
        down_price=down_price,
        down_pnl=total_payoff(legs, down_price),
        flat_pnl=total_payoff(legs, candidate.underlying_price),
        up_price=up_price,
        up_pnl=total_payoff(legs, up_price),
    )


def _explain_with_scenario(
    ticker: str, candidate: StrategyCandidate, implied_move_pct: Decimal, scenario: ScenarioPnl
) -> str:
    move_pct_str = f"{implied_move_pct * 100:.1f}%"
    max_loss = candidate.analysis.max_loss
    return (
        f"If {ticker} moves ±{move_pct_str} (the options market's current implied move), "
        f"this {candidate.category.value.replace('_', ' ')} would be worth "
        f"${scenario.down_pnl:,.2f} on a move down to ${scenario.down_price:,.2f} and "
        f"${scenario.up_pnl:,.2f} on a move up to ${scenario.up_price:,.2f}"
        + (
            f", against a maximum possible loss of ${max_loss:,.2f}."
            if max_loss is not None
            else "."
        )
    )


def _explain_fallback(ticker: str, candidate: StrategyCandidate) -> str:
    max_loss = candidate.analysis.max_loss
    return (
        f"No real options-implied move is on record yet for {ticker}'s upcoming earnings, so "
        f"this {candidate.category.value.replace('_', ' ')} is ranked by capital at risk alone: "
        f"a maximum possible loss of ${max_loss:,.2f}."
        if max_loss is not None
        else f"No real options-implied move or bounded max loss is available for this "
        f"{candidate.category.value.replace('_', ' ')} yet."
    )


def rank_strategy_candidates(
    candidates: list[StrategyCandidate], ticker: str, implied_move_pct: Decimal | None
) -> list[RankedStrategy]:
    """Ranks every candidate best-first. ``implied_move_pct`` should be the
    company's real, most recently computed VolatilitySnapshot.implied_move_pct
    (None when no options-implied move has been computed yet -- see
    services/options_analytics.py) -- never a guessed or default value.
    """
    scored: list[tuple[Decimal, StrategyCandidate, ScenarioPnl | None, str]] = []

    for candidate in candidates:
        max_loss = candidate.analysis.max_loss
        if implied_move_pct is not None:
            scenario = _scenario_pnl(candidate, implied_move_pct)
            avg_pnl = (scenario.down_pnl + scenario.up_pnl) / 2
            score = avg_pnl / max_loss if max_loss else avg_pnl
            explanation = _explain_with_scenario(ticker, candidate, implied_move_pct, scenario)
        else:
            scenario = None
            # Ascending max_loss = "safest first"; negated so a single
            # descending sort (higher score first) works for both tiers.
            score = -max_loss if max_loss is not None else Decimal("-Infinity")
            explanation = _explain_fallback(ticker, candidate)
        scored.append((score, candidate, scenario, explanation))

    scored.sort(key=lambda item: item[0], reverse=True)

    return [
        RankedStrategy(
            candidate=candidate, rank=i, scenario=scenario, score=score, explanation=explanation
        )
        for i, (score, candidate, scenario, explanation) in enumerate(scored, start=1)
    ]
