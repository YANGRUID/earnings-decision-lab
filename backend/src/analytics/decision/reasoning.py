"""Deterministic "why this strategy" / "main risks" bullet generation
(Phase 14.9 Part E). Every bullet below is built from numbers already
computed elsewhere in this package or in analytics/options -- never
invented, never an LLM call. The LLM (services/decision_engine.py) may
wrap these in prose elsewhere in the response, but the factual bullets
themselves come from here, deterministically, so they can never drift
from the real numbers shown alongside them.
"""

from decimal import Decimal

from analytics.decision.strategy_scoring import ViewRankedStrategy
from analytics.options.strategy_candidates import StrategyCandidate
from models.enums import DecisionDirection

_DIRECTION_LABEL: dict[DecisionDirection, str] = {
    DecisionDirection.STRONG_BULLISH: "strongly bullish",
    DecisionDirection.BULLISH: "moderately bullish",
    DecisionDirection.NEUTRAL: "neutral",
    DecisionDirection.BEARISH: "moderately bearish",
    DecisionDirection.STRONG_BEARISH: "strongly bearish",
}


def _pct(value: Decimal) -> str:
    return f"{value * 100:.1f}%"


def _money(value: Decimal) -> str:
    return f"${value:,.2f}"


def build_why_bullets(
    ranked: ViewRankedStrategy,
    *,
    direction: DecisionDirection,
    implied_move_pct: Decimal | None,
    has_bid_ask: bool,
) -> list[str]:
    candidate = ranked.candidate
    bullets: list[str] = [f"The stated view is {_DIRECTION_LABEL[direction]}."]

    if candidate.analysis.breakevens:
        underlying = candidate.underlying_price
        nearest = min(candidate.analysis.breakevens, key=lambda be: abs(be - underlying))
        direction_word = "up" if nearest >= underlying else "down"
        required_pct = abs(nearest - underlying) / underlying
        bullets.append(f"Break-even requires a move {direction_word} of {_pct(required_pct)}.")

    if implied_move_pct is not None:
        bullets.append(f"The current options-implied move is ±{_pct(implied_move_pct)}.")

    compat = ranked.move_compatibility
    if compat is not None and compat.sample_size > 0:
        verb = "met or exceeded" if compat.requires_move_beyond_threshold else "stayed within"
        bullets.append(
            f"{compat.compatible_count} of {compat.sample_size} real past earnings moves "
            f"({_pct(compat.compatible_pct)}) {verb} the required move."
        )

    max_loss = candidate.analysis.max_loss
    if max_loss is not None:
        bullets.append(f"Risk is capped at {_money(max_loss)}.")

    if candidate.analysis.net_premium < 0:
        bullets.append(
            f"This is a net credit of {_money(abs(candidate.analysis.net_premium))} received "
            "up front."
        )

    if has_bid_ask:
        bullets.append("Real bid/ask quotes were available for the legs used.")
    else:
        bullets.append(
            "Real bid/ask quotes were not available for these legs -- prices used are "
            "derived (mid-price or last trade), not a live tradable quote."
        )

    return bullets


def build_risk_bullets(ranked: ViewRankedStrategy) -> list[str]:
    candidate = ranked.candidate
    analysis = candidate.analysis
    bullets: list[str] = []

    if analysis.max_loss is None:
        bullets.append(
            "This position has undefined/unbounded risk -- no finite maximum loss can be "
            "stated."
        )
    elif analysis.net_premium >= 0:
        bullets.append(
            f"A flat or unfavorable reaction can lose some or all of the "
            f"{_money(analysis.net_premium)} paid to enter."
        )
    else:
        bullets.append(
            "A large adverse move can exceed the credit received and reach the full "
            f"{_money(analysis.max_loss)} maximum loss."
        )

    if analysis.max_profit is not None:
        bullets.append(f"Profit is capped at {_money(analysis.max_profit)}.")

    if ranked.move_compatibility is not None and ranked.move_compatibility.sample_size < 5:
        bullets.append(
            f"Only {ranked.move_compatibility.sample_size} real past earnings events are on "
            "record for this comparison -- a small sample."
        )

    bullets.append(
        "Implied volatility can fall sharply right after earnings (\"IV crush\"), which can "
        "reduce the value of long option legs even on a favorable move."
    )

    return bullets


def strategy_category_label(candidate: StrategyCandidate) -> str:
    return candidate.category.value.replace("_", " ").title()
