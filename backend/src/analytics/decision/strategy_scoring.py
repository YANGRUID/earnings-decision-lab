"""Deterministic, view-aware strategy scoring for the AI Options Decision
Engine (Phase 14.9) -- distinct from, and never replacing,
analytics/options/strategy_ranking.py's direction-agnostic Strategy Lab
ranking (which stays exactly as-is for that page). This module answers a
different question: "given a stated direction/volatility view, how well
does each real candidate express it" -- every component below is computed
from numbers this project already has (real strikes/premiums, the real
options-implied move, real historical earnings moves, the real options
snapshot's own data-quality signal), never an LLM judgment and never a
probability of profit. See STRATEGY SCORE METHODOLOGY in the module
docstring of confidence.py for the sibling (but separate) confidence
score -- that one is about the *view*, this one is about a *strategy's
fit to that view*, and the two must not be confused with each other.

Total score is /100 across six components, labeled "Model Strategy Score"
everywhere it's surfaced -- never presented as a probability of winning.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from analytics.options.move_compatibility import MoveCompatibility, assess_move_compatibility
from analytics.options.payoff import total_payoff
from analytics.options.strategy_candidates import StrategyCandidate, StrategyCategory
from models.enums import (
    DecisionDirection,
    DecisionVolatilityView,
    RiskProfile,
    StrategyRiskPreference,
)

# Every category this project generates has a defined max loss (net
# premium paid) by construction (see strategy_candidates.py) -- there is
# currently no naked/uncovered short category to exclude even at the
# most permissive tier. DEFINED_RISK_ONLY additionally excludes the two
# single-leg long categories: not because they have undefined risk (they
# don't -- max loss is the premium paid), but because a single long
# option is a materially different risk/theta profile than a spread, and
# the owner may want to exclude that style specifically. See Part D
# section 15.
_SINGLE_LEG_LONG_CATEGORIES = frozenset({StrategyCategory.LONG_CALL, StrategyCategory.LONG_PUT})


def filter_candidates_by_risk_preference(
    candidates: list[StrategyCandidate], risk_preference: StrategyRiskPreference
) -> list[StrategyCandidate]:
    if risk_preference == StrategyRiskPreference.DEFINED_RISK_ONLY:
        return [c for c in candidates if c.category not in _SINGLE_LEG_LONG_CATEGORIES]
    return list(candidates)


# Rebalanced again in Options Decision Engine V3 Part F to add
# expiration_fit and risk_profile_fit as real, scored components (Part
# 36) -- proportionally scaled down from the Phase 14.10 Part E3 weights
# below so every existing component keeps the same relative importance to
# each other, just making room for the two new ones. direction_fit and
# volatility_fit are the two "view fit" components; breakeven_fit/
# historical_fit/risk_reward are the three "structural quality"
# components; expiration_fit and risk_profile_fit are the two new
# "context fit" components (does this expiration/structure suit the
# situation, independent of the stated view); liquidity/data_quality are
# the two "data confidence" components.
WEIGHT_DIRECTION_FIT = 15
WEIGHT_VOLATILITY_FIT = 11
WEIGHT_EXPIRATION_FIT = 10
WEIGHT_BREAKEVEN_FIT = 12
WEIGHT_HISTORICAL_FIT = 12
WEIGHT_RISK_REWARD = 12
WEIGHT_RISK_PROFILE_FIT = 8
WEIGHT_LIQUIDITY = 10
WEIGHT_DATA_QUALITY = 10

assert (
    WEIGHT_DIRECTION_FIT
    + WEIGHT_VOLATILITY_FIT
    + WEIGHT_EXPIRATION_FIT
    + WEIGHT_BREAKEVEN_FIT
    + WEIGHT_HISTORICAL_FIT
    + WEIGHT_RISK_REWARD
    + WEIGHT_RISK_PROFILE_FIT
    + WEIGHT_LIQUIDITY
    + WEIGHT_DATA_QUALITY
    == 100
)

# Same DTE sweet-spot rule as analytics/options/expiration_selection.py's
# _dte_suitability_score (Part C) -- reused here as a documented constant
# rather than importing that module's internal, since this component asks
# a narrower question ("is this ONE candidate's own expiration
# reasonable") than the Expiration Engine's full multi-candidate
# discovery/comparison, which decision generation doesn't run today (see
# Part H/I -- wiring Auto/Manual expiration selection into AI Decision
# itself is separate, still-pending work).
_EXPIRATION_DTE_SWEET_SPOT_LOW = 7
_EXPIRATION_DTE_SWEET_SPOT_HIGH = 21
_EXPIRATION_DTE_MAX_CONSIDERED = 60

# Options Decision Engine V3 Part 22: Aggressive explicitly favors "more
# directional structures" (single-leg long calls/puts) over defined-risk
# spreads -- the only real, defensible differentiation among the
# categories every profile already allows once eligibility filtering
# (analytics/decision/risk_profile.py) has run. Conservative/Moderate
# don't get an equivalent single-category preference: Conservative never
# even sees single-leg-long candidates (filtered out before scoring
# reaches them), and Moderate's "long call/put when evidence supports it"
# is already what direction_fit/volatility_fit exist to judge.
_AGGRESSIVE_PREFERRED_CATEGORIES = frozenset(
    {StrategyCategory.LONG_CALL, StrategyCategory.LONG_PUT}
)

# How far (as a fraction of the real implied move) each direction targets
# -- "strong" views target the full magnitude of the market's own implied
# move, moderate views target half of it, neutral targets no move at all.
# This reuses the one real move-magnitude number this project has
# (VolatilitySnapshot.implied_move_pct) rather than inventing a new one.
_DIRECTION_TARGET_FRACTION: dict[DecisionDirection, Decimal] = {
    DecisionDirection.STRONG_BULLISH: Decimal("1.0"),
    DecisionDirection.BULLISH: Decimal("0.5"),
    DecisionDirection.NEUTRAL: Decimal("0.0"),
    DecisionDirection.BEARISH: Decimal("-0.5"),
    DecisionDirection.STRONG_BEARISH: Decimal("-1.0"),
}


@dataclass(frozen=True)
class StrategyScoreBreakdown:
    direction_fit: int
    volatility_fit: int
    expiration_fit: int
    breakeven_fit: int
    historical_fit: int
    risk_reward: int
    risk_profile_fit: int
    liquidity: int
    data_quality: int

    @property
    def total(self) -> int:
        return (
            self.direction_fit
            + self.volatility_fit
            + self.expiration_fit
            + self.breakeven_fit
            + self.historical_fit
            + self.risk_reward
            + self.risk_profile_fit
            + self.liquidity
            + self.data_quality
        )

    def as_dict(self) -> dict[str, int]:
        return {
            "direction_fit": self.direction_fit,
            "volatility_fit": self.volatility_fit,
            "expiration_fit": self.expiration_fit,
            "breakeven_fit": self.breakeven_fit,
            "historical_fit": self.historical_fit,
            "risk_reward": self.risk_reward,
            "risk_profile_fit": self.risk_profile_fit,
            "liquidity": self.liquidity,
            "data_quality": self.data_quality,
        }


@dataclass(frozen=True)
class ViewRankedStrategy:
    candidate: StrategyCandidate
    rank: int  # 1-indexed, 1 = best fit for the stated view
    target_price: Decimal | None  # None only when no implied move is on record
    payoff_at_target: Decimal | None
    score: StrategyScoreBreakdown
    move_compatibility: MoveCompatibility | None


def target_price_for_direction(
    underlying_price: Decimal, implied_move_pct: Decimal | None, direction: DecisionDirection
) -> Decimal | None:
    if implied_move_pct is None:
        return None
    fraction = _DIRECTION_TARGET_FRACTION[direction]
    return underlying_price * (1 + fraction * implied_move_pct)


def _direction_fit(candidate: StrategyCandidate, payoff_at_target: Decimal | None) -> int:
    if payoff_at_target is None:
        return round(WEIGHT_DIRECTION_FIT * Decimal("0.3"))  # no implied move -- can't assess
    max_profit = candidate.analysis.max_profit
    if payoff_at_target <= 0:
        return 0
    if max_profit is None or max_profit == 0:
        # Unbounded or zero max_profit but a positive payoff at target --
        # a real, favorable structural fact, scored generously but not
        # maxed (there's no ceiling to compare against).
        return round(WEIGHT_DIRECTION_FIT * Decimal("0.8"))
    fraction = min(payoff_at_target / max_profit, Decimal(1))
    return round(fraction * WEIGHT_DIRECTION_FIT)


def _volatility_fit(candidate: StrategyCandidate, volatility_view: DecisionVolatilityView) -> int:
    """Real signal: net_premium's sign is a structural fact about whether
    the candidate is a net-debit (long options premium, profits from a
    large realized move regardless of direction -- long call/put,
    straddle, strangle, long call butterfly's debit) or net-credit
    (short options premium, profits from the underlying staying inside a
    range -- credit spreads, iron condor, iron butterfly) position. This
    is the same real fact rank_candidates_for_view's own tiebreaker
    already used -- promoted here to a real, weighted score component
    (rather than only breaking ties on an already-identical total) so a
    Neutral+Long-Vol view can no longer rank a short-vol credit structure
    first purely on direction_fit; see Part E3.

    NEUTRAL_VOL carries no opinion on premium sign either way, so it gets
    a fixed mid-weight regardless of the candidate's own structure.
    """
    if volatility_view == DecisionVolatilityView.NEUTRAL_VOL:
        return round(WEIGHT_VOLATILITY_FIT * Decimal("0.5"))
    net_premium = candidate.analysis.net_premium
    if volatility_view == DecisionVolatilityView.LONG_VOL:
        return WEIGHT_VOLATILITY_FIT if net_premium > 0 else 0
    return WEIGHT_VOLATILITY_FIT if net_premium < 0 else 0


def _breakeven_fit(candidate: StrategyCandidate, implied_move_pct: Decimal | None) -> int:
    if not candidate.analysis.breakevens:
        return 0
    if implied_move_pct is None or implied_move_pct == 0:
        return round(WEIGHT_BREAKEVEN_FIT * Decimal("0.3"))  # no data -- low, not zero
    underlying = candidate.underlying_price
    required_move_pct = min(
        abs(be - underlying) / underlying for be in candidate.analysis.breakevens
    )
    ratio = required_move_pct / implied_move_pct
    # Needing well under the market's own implied move to break even is a
    # real structural advantage; needing well beyond it is a real
    # structural disadvantage. Linear falloff, capped at the full weight.
    fraction = max(Decimal(0), min(Decimal(1), 1 - (ratio - Decimal("0.5"))))
    return round(fraction * WEIGHT_BREAKEVEN_FIT)


def _historical_fit(compatibility: MoveCompatibility | None) -> int:
    if compatibility is None:
        return round(WEIGHT_HISTORICAL_FIT * Decimal("0.25"))  # no history -- low, not zero
    return round(compatibility.compatible_pct * WEIGHT_HISTORICAL_FIT)


def _risk_reward(candidate: StrategyCandidate) -> int:
    max_loss = candidate.analysis.max_loss
    if max_loss is None:
        return 0  # undefined risk -- never scored favorably here
    ror = candidate.analysis.return_on_risk
    if ror is None:
        # max_loss is defined but max_profit is unbounded (e.g. a long
        # call/put/straddle/strangle) -- a real, favorable asymmetry.
        return round(WEIGHT_RISK_REWARD * Decimal("0.75"))
    fraction = min(ror / Decimal("2"), Decimal(1))
    return round(fraction * WEIGHT_RISK_REWARD)


def _liquidity(has_bid_ask: bool) -> int:
    return WEIGHT_LIQUIDITY if has_bid_ask else 0


def _data_quality(market_data_quality: str | None) -> int:
    mapping = {"live": 1.0, "delayed": Decimal("0.8"), "frozen": Decimal("0.4")}
    fraction = mapping.get(market_data_quality or "", Decimal(0))
    return round(Decimal(str(fraction)) * WEIGHT_DATA_QUALITY)


def _expiration_fit(candidate: StrategyCandidate, earnings_date: date | None) -> int:
    """Options Decision Engine V3 Part 12/36: does this ONE candidate's
    own real expiration sit in a reasonable DTE window relative to the
    real earnings date -- full weight in a 7-21-day-after-earnings sweet
    spot (enough time for the event to matter without excessive extra
    time premium), decaying toward the edges. When no earnings date is
    known (general/current mode -- see analytics/options/implied_move.py),
    there's no event to judge "days after" against at all, so this scores
    at a fixed neutral fraction rather than fabricating a reference date,
    matching this file's existing "no data -- low, not zero" convention
    (see _breakeven_fit/_historical_fit)."""
    if earnings_date is None:
        return round(WEIGHT_EXPIRATION_FIT * Decimal("0.5"))

    days_after_earnings = (candidate.expiration - earnings_date).days
    if days_after_earnings < 1:
        return 0  # doesn't even cover the event -- never scored favorably
    if _EXPIRATION_DTE_SWEET_SPOT_LOW <= days_after_earnings <= _EXPIRATION_DTE_SWEET_SPOT_HIGH:
        return WEIGHT_EXPIRATION_FIT
    if days_after_earnings < _EXPIRATION_DTE_SWEET_SPOT_LOW:
        fraction = Decimal(days_after_earnings) / Decimal(_EXPIRATION_DTE_SWEET_SPOT_LOW)
        return round(WEIGHT_EXPIRATION_FIT * max(Decimal("0.3"), fraction))
    excess = min(
        days_after_earnings - _EXPIRATION_DTE_SWEET_SPOT_HIGH,
        _EXPIRATION_DTE_MAX_CONSIDERED - _EXPIRATION_DTE_SWEET_SPOT_HIGH,
    )
    fraction = 1 - Decimal(excess) / Decimal(
        _EXPIRATION_DTE_MAX_CONSIDERED - _EXPIRATION_DTE_SWEET_SPOT_HIGH
    )
    return round(WEIGHT_EXPIRATION_FIT * max(Decimal("0"), fraction))


def _risk_profile_fit(candidate: StrategyCandidate, risk_profile: RiskProfile | None) -> int:
    """Options Decision Engine V3 Part 20-23/36: full weight for every
    category every profile already allows (eligibility itself is a hard
    gate in analytics/decision/risk_profile.py, not this score) -- the
    one real differentiation defensible without inventing a fake
    precision is Aggressive's documented preference for single-leg
    long/high-gamma structures over defined-risk spreads (Part 22). No
    risk_profile given (e.g. Strategy Lab's direction-agnostic ranking
    never sets one) scores neutrally at full weight."""
    if risk_profile is None or risk_profile != RiskProfile.AGGRESSIVE:
        return WEIGHT_RISK_PROFILE_FIT
    if candidate.category in _AGGRESSIVE_PREFERRED_CATEGORIES:
        return WEIGHT_RISK_PROFILE_FIT
    return round(WEIGHT_RISK_PROFILE_FIT * Decimal("0.6"))


def score_candidate_for_view(
    candidate: StrategyCandidate,
    *,
    direction: DecisionDirection,
    volatility_view: DecisionVolatilityView,
    implied_move_pct: Decimal | None,
    historical_move_pcts: list[Decimal],
    has_bid_ask: bool,
    market_data_quality: str | None,
    earnings_date: date | None = None,
    risk_profile: RiskProfile | None = None,
) -> tuple[StrategyScoreBreakdown, Decimal | None, Decimal | None, MoveCompatibility | None]:
    """Returns (breakdown, target_price, payoff_at_target, move_compatibility)."""
    target_price = target_price_for_direction(
        candidate.underlying_price, implied_move_pct, direction
    )
    payoff_at_target = (
        total_payoff(list(candidate.legs), target_price) if target_price is not None else None
    )
    compatibility = assess_move_compatibility(candidate, historical_move_pcts)

    breakdown = StrategyScoreBreakdown(
        direction_fit=_direction_fit(candidate, payoff_at_target),
        volatility_fit=_volatility_fit(candidate, volatility_view),
        expiration_fit=_expiration_fit(candidate, earnings_date),
        breakeven_fit=_breakeven_fit(candidate, implied_move_pct),
        historical_fit=_historical_fit(compatibility),
        risk_reward=_risk_reward(candidate),
        risk_profile_fit=_risk_profile_fit(candidate, risk_profile),
        liquidity=_liquidity(has_bid_ask),
        data_quality=_data_quality(market_data_quality),
    )
    return breakdown, target_price, payoff_at_target, compatibility


def rank_candidates_for_view(
    candidates: list[StrategyCandidate],
    *,
    direction: DecisionDirection,
    volatility_view: DecisionVolatilityView,
    implied_move_pct: Decimal | None,
    historical_move_pcts: list[Decimal],
    has_bid_ask: bool,
    market_data_quality: str | None,
    earnings_date: date | None = None,
    risk_profile: RiskProfile | None = None,
) -> list[ViewRankedStrategy]:
    """Ranks every candidate best-first for the stated direction AND
    volatility view -- volatility_fit (see _volatility_fit) is a real,
    weighted component of each candidate's total score, not just a
    tiebreaker, so a Neutral+Long-Vol view no longer ranks a short-vol
    credit structure first purely on direction_fit (Part E3). The
    remaining net_premium-sign check below only breaks ties between
    candidates whose *total* score is still identical after that."""

    def volatility_alignment(candidate: StrategyCandidate) -> int:
        if volatility_view == DecisionVolatilityView.LONG_VOL:
            return 1 if candidate.analysis.net_premium > 0 else 0
        if volatility_view == DecisionVolatilityView.SHORT_VOL:
            return 1 if candidate.analysis.net_premium < 0 else 0
        return 0

    scored = []
    for candidate in candidates:
        breakdown, target_price, payoff_at_target, compatibility = score_candidate_for_view(
            candidate,
            direction=direction,
            volatility_view=volatility_view,
            implied_move_pct=implied_move_pct,
            historical_move_pcts=historical_move_pcts,
            has_bid_ask=has_bid_ask,
            market_data_quality=market_data_quality,
            earnings_date=earnings_date,
            risk_profile=risk_profile,
        )
        scored.append((breakdown, target_price, payoff_at_target, compatibility, candidate))

    scored.sort(key=lambda item: (item[0].total, volatility_alignment(item[4])), reverse=True)

    return [
        ViewRankedStrategy(
            candidate=candidate,
            rank=i,
            target_price=target_price,
            payoff_at_target=payoff_at_target,
            score=breakdown,
            move_compatibility=compatibility,
        )
        for i, (breakdown, target_price, payoff_at_target, compatibility, candidate) in enumerate(
            scored, start=1
        )
    ]
