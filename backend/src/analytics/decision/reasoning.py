"""Deterministic "why this strategy" / "main risks" bullet generation
(Phase 14.9 Part E), extended in Options Decision Engine V3 Part G with
"why this expiration," "why these strikes," and "why not alternative #2."
Every bullet below is built from numbers already computed elsewhere in
this package or in analytics/options -- never invented, never an LLM
call. The LLM (services/decision_engine.py) may wrap these in prose
elsewhere in the response, but the factual bullets themselves come from
here, deterministically, so they can never drift from the real numbers
shown alongside them.
"""

from datetime import date
from decimal import Decimal

from analytics.decision.strategy_scoring import ViewRankedStrategy
from analytics.options.strategy_candidates import StrategyCandidate, StrategyCategory
from models.enums import DecisionDirection, RiskProfile

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


_RISK_PROFILE_LABEL: dict[RiskProfile, str] = {
    RiskProfile.CONSERVATIVE: "Conservative",
    RiskProfile.MODERATE: "Moderate",
    RiskProfile.AGGRESSIVE: "Aggressive",
}


def build_expiration_bullets(ranked: ViewRankedStrategy, earnings_date: date | None) -> list[str]:
    """Options Decision Engine V3 Part G/41: "why this expiration" --
    every claim is the same real DTE/days-after-earnings numbers
    analytics/decision/strategy_scoring.py::_expiration_fit already scored
    this candidate on, restated in words rather than a bare score."""
    candidate = ranked.candidate
    bullets: list[str] = []
    if earnings_date is None:
        bullets.append(
            f"{candidate.expiration.isoformat()} was used with no confirmed earnings date on "
            "record -- the nearest listed expiration, not anchored to a specific event."
        )
        return bullets

    days_after = (candidate.expiration - earnings_date).days
    if days_after < 1:
        bullets.append(
            f"{candidate.expiration.isoformat()} is on or before the {earnings_date.isoformat()} "
            "earnings date and does not cover the event."
        )
        return bullets
    bullets.append(
        f"{candidate.expiration.isoformat()} is {days_after} day(s) after the "
        f"{earnings_date.isoformat()} earnings date."
    )
    if 7 <= days_after <= 21:
        bullets.append(
            "That falls inside the 7-21 day window this project treats as a reasonable "
            "balance between covering the event and not paying for excess time premium."
        )
    elif days_after < 7:
        bullets.append(
            f"That is a short {days_after}-day window after the event, which carries more "
            "gamma risk than a longer-dated expiration would."
        )
    else:
        bullets.append(
            f"That carries {days_after - 21} more day(s) of time premium beyond this "
            "project's 21-day reference window."
        )
    return bullets


def build_strike_bullets(ranked: ViewRankedStrategy) -> list[str]:
    """Options Decision Engine V3 Part G/41: "why these strikes" -- built
    only from real strike/underlying/breakeven numbers already on the
    candidate; never references delta (StrategyCandidate's legs don't
    carry Greeks -- see analytics/options/strategy_candidates.py -- so a
    delta-based claim here would not be grounded in a real stored value)."""
    candidate = ranked.candidate
    underlying = candidate.underlying_price
    bullets: list[str] = []
    for leg in candidate.legs:
        distance_pct = abs(leg.strike - underlying) / underlying
        side = "above" if leg.strike >= underlying else "below"
        bullets.append(
            f"{leg.action.value.title()} {leg.quantity} {leg.option_type.value} at "
            f"{_money(leg.strike)}, {_pct(distance_pct)} {side} the {_money(underlying)} "
            "underlying."
        )
    if candidate.analysis.breakevens:
        nearest = min(candidate.analysis.breakevens, key=lambda be: abs(be - underlying))
        required_pct = abs(nearest - underlying) / underlying
        bullets.append(
            f"These strikes put the nearest breakeven at {_money(nearest)}, a "
            f"{_pct(required_pct)} move from the current price."
        )
    return bullets


def build_risk_profile_fit_bullets(
    ranked: ViewRankedStrategy, risk_profile: RiskProfile | None
) -> list[str]:
    """Options Decision Engine V3 Part G/40: "why this risk level fit" --
    states the real eligibility/scoring facts already applied by
    analytics/decision/risk_profile.py and _risk_profile_fit, never a new
    judgment."""
    if risk_profile is None:
        return []
    label = _RISK_PROFILE_LABEL[risk_profile]
    bullets = [f"This candidate is eligible under the {label} risk profile."]
    if risk_profile == RiskProfile.CONSERVATIVE:
        bullets.append(
            "Conservative excludes single-leg long calls/puts entirely -- only defined-risk "
            "spread/condor/butterfly structures are ever considered."
        )
    elif risk_profile == RiskProfile.AGGRESSIVE:
        if ranked.candidate.category in {StrategyCategory.LONG_CALL, StrategyCategory.LONG_PUT}:
            bullets.append(
                "Aggressive favors single-leg long structures like this one for their higher "
                "gamma/directional exposure."
            )
    return bullets


def build_why_not_alternative_bullets(
    top: ViewRankedStrategy, alternative: ViewRankedStrategy
) -> list[str]:
    """Options Decision Engine V3 Part 42: a real, numeric comparison of
    the #1 recommendation against one specific alternative -- every
    bullet cites the actual difference in cost, risk, or score between
    these two real candidates, never a generic templated line that would
    read the same regardless of which two candidates were compared."""
    top_analysis = top.candidate.analysis
    alt_analysis = alternative.candidate.analysis
    bullets: list[str] = []

    top_cost = abs(top_analysis.net_premium)
    alt_cost = abs(alt_analysis.net_premium)
    if alt_cost > 0 and top_cost > 0 and alt_cost != top_cost:
        ratio = alt_cost / top_cost
        cheaper = "#1" if top_cost < alt_cost else "#2"
        bullets.append(
            f"{strategy_category_label(alternative.candidate)} costs "
            f"{ratio if cheaper == '#1' else 1 / ratio:.1f}x "
            f"{'more' if cheaper == '#1' else 'less'} premium than "
            f"{strategy_category_label(top.candidate)} "
            f"({_money(alt_cost)} vs. {_money(top_cost)})."
        )

    if top_analysis.max_loss is not None and alt_analysis.max_loss is not None:
        if top_analysis.max_loss != alt_analysis.max_loss:
            bullets.append(
                f"Max loss is {_money(top_analysis.max_loss)} for #1 vs. "
                f"{_money(alt_analysis.max_loss)} for #2."
            )
    elif top_analysis.max_loss is not None and alt_analysis.max_loss is None:
        bullets.append(
            f"#1 has a defined max loss of {_money(top_analysis.max_loss)}; #2 has "
            "undefined/unbounded risk."
        )
    elif top_analysis.max_loss is None and alt_analysis.max_loss is not None:
        bullets.append(
            f"#2 has a defined max loss of {_money(alt_analysis.max_loss)}; #1 has "
            "undefined/unbounded risk -- #1 is still ranked higher on its other score "
            "components."
        )

    score_gap = top.score.total - alternative.score.total
    if score_gap > 0:
        biggest_component, biggest_gap = max(
            (
                (name, getattr(top.score, name) - getattr(alternative.score, name))
                for name in top.score.as_dict()
                if name != "total"
            ),
            key=lambda item: item[1],
        )
        if biggest_gap > 0:
            bullets.append(
                f"#1 leads #2 by {score_gap} total score points, driven mostly by "
                f"{biggest_component.replace('_', ' ')} ({biggest_gap} point(s) higher)."
            )

    if top.move_compatibility is not None and alternative.move_compatibility is not None:
        top_compat = top.move_compatibility
        alt_compat = alternative.move_compatibility
        if top_compat.compatible_pct != alt_compat.compatible_pct:
            bullets.append(
                f"Historically, {top_compat.compatible_count}/{top_compat.sample_size} real "
                f"past earnings moves would have suited #1 vs. "
                f"{alt_compat.compatible_count}/{alt_compat.sample_size} for #2."
            )

    return bullets
