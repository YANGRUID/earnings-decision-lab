"""Deterministic expiration-selection domain model (Options Decision Engine
V3, Part C). Pure functions over already-fetched quotes for one candidate
expiration at a time -- no I/O, no provider calls. The service layer
(services/expiration_engine.py) is responsible for discovering real
candidate expirations and fetching each one's chain; this module only
scores and explains what it's given.

Today's expiration rule (analytics/options/implied_move.py's
select_expiration_after / select_nearest_listed_expiration) always picks
exactly one expiration with no comparison against alternatives. This module
adds that comparison layer on top, without replacing the underlying pick
rule -- the nearest valid post-earnings expiration is still always one of
the candidates considered.
"""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Literal

from providers.types import OptionQuote

ExpirationQuality = Literal["good", "acceptable", "poor", "untradeable"]

# Same thresholds as services/options_reconstruction.py::classify_chain_quality
# (Phase 14.13 Part 16) -- duplicated rather than imported since analytics/
# must not depend on services/ (services depends on analytics/, never the
# reverse). Kept in sync deliberately; both are one-line-per-threshold and
# unlikely to drift silently.
def _classify_quality(priceable_ratio: float) -> ExpirationQuality:
    if priceable_ratio <= 0:
        return "untradeable"
    if priceable_ratio >= 0.8:
        return "good"
    if priceable_ratio >= 0.4:
        return "acceptable"
    return "poor"


_DATA_QUALITY_SCORE: dict[str, Decimal] = {
    "live": Decimal("1.0"),
    "delayed": Decimal("0.8"),
    "frozen": Decimal("0.4"),
}

# Score component weights, sum to 100 -- mirrors the style of
# analytics/decision/strategy_scoring.py's WEIGHT_* constants.
WEIGHT_EVENT_FIT = 25
WEIGHT_LIQUIDITY = 20
WEIGHT_QUOTE_COVERAGE = 15
WEIGHT_BID_ASK_QUALITY = 15
WEIGHT_DTE_SUITABILITY = 15
WEIGHT_DATA_QUALITY = 10
_TOTAL_WEIGHT = (
    WEIGHT_EVENT_FIT
    + WEIGHT_LIQUIDITY
    + WEIGHT_QUOTE_COVERAGE
    + WEIGHT_BID_ASK_QUALITY
    + WEIGHT_DTE_SUITABILITY
    + WEIGHT_DATA_QUALITY
)
assert _TOTAL_WEIGHT == 100  # noqa: S101 -- module-level invariant, not a test assertion

# DTE "sweet spot" used by _dte_suitability -- documented, not overfit: a
# post-earnings expiration with roughly one to three weeks of remaining
# life balances theta burden against not paying for excess time premium.
# Strategy-family-specific tuning (Part 12: long vol wants more time,
# short-DTE short-vol structures carry more gamma risk) is intentionally
# NOT implemented yet -- this is a single general-purpose curve applied
# uniformly, called out explicitly in reasons/limitations rather than
# silently pretending to be strategy-aware.
_DTE_SWEET_SPOT_LOW = 7
_DTE_SWEET_SPOT_HIGH = 21
_DTE_MAX_CONSIDERED = 60  # beyond this, DTE suitability floors at 0


@dataclass(frozen=True)
class ExpirationScoreBreakdown:
    event_fit: int
    liquidity: int
    quote_coverage: int
    bid_ask_quality: int
    dte_suitability: int
    data_quality: int

    @property
    def total(self) -> int:
        return (
            self.event_fit
            + self.liquidity
            + self.quote_coverage
            + self.bid_ask_quality
            + self.dte_suitability
            + self.data_quality
        )

    def as_dict(self) -> dict[str, int]:
        return {
            "event_fit": self.event_fit,
            "liquidity": self.liquidity,
            "quote_coverage": self.quote_coverage,
            "bid_ask_quality": self.bid_ask_quality,
            "dte_suitability": self.dte_suitability,
            "data_quality": self.data_quality,
            "total": self.total,
        }


@dataclass(frozen=True)
class ExpirationCandidate:
    expiration: date
    dte: int
    days_after_earnings: int | None
    contract_count: int
    priceable_contract_count: int
    quote_coverage: Decimal
    bid_ask_coverage: Decimal
    oi_coverage: Decimal
    volume_coverage: Decimal
    atm_iv: Decimal | None
    atm_spread_pct: Decimal | None
    quality: ExpirationQuality
    score: ExpirationScoreBreakdown
    is_earnings_anchored: bool
    # True only for a candidate whose expiration is on/before the earnings
    # date (never selectable in Auto mode -- see resolve_auto_expiration --
    # but real, listed, and shown if a caller passes it through explicitly).
    excluded_pre_earnings: bool = False


def _find_atm_quotes(
    quotes: list[OptionQuote], underlying_price: Decimal | None
) -> list[OptionQuote]:
    if not quotes or underlying_price is None:
        return []
    strikes = sorted({q.strike for q in quotes})
    if not strikes:
        return []
    atm_strike = min(strikes, key=lambda s: abs(s - underlying_price))
    return [q for q in quotes if q.strike == atm_strike]


def _atm_iv(atm_quotes: list[OptionQuote]) -> Decimal | None:
    ivs = [q.implied_volatility for q in atm_quotes if q.implied_volatility is not None]
    if not ivs:
        return None
    return sum(ivs, start=Decimal(0)) / len(ivs)


def _atm_spread_pct(atm_quotes: list[OptionQuote]) -> Decimal | None:
    spreads = []
    for q in atm_quotes:
        if q.bid is not None and q.ask is not None and q.bid > 0 and q.ask > 0:
            mid = (q.bid + q.ask) / 2
            spreads.append((q.ask - q.bid) / mid)
    if not spreads:
        return None
    return sum(spreads, start=Decimal(0)) / len(spreads)


def _event_fit_score(dte: int, days_after_earnings: int | None) -> int:
    """Full weight for any expiration that genuinely covers the earnings
    event (days_after_earnings >= 1, i.e. the event falls inside the
    contract's remaining life -- consistent with select_expiration_after's
    strictly-after rule). Decays mildly for excess time carried well beyond
    the event (more unpriced-for time premium, per Part 11: "prefer
    expirations that contain the event without unnecessary excess time
    premium -- but do not assume shorter DTE is always better"). When no
    earnings date is known at all, event fit doesn't apply -- full weight,
    since there's no event to have missed."""
    if days_after_earnings is None:
        return WEIGHT_EVENT_FIT
    if days_after_earnings < 1:
        return 0
    # Full weight through 5 days after the event (a same-week expiration is
    # never "too far"); mild linear decay from there through 30 days out,
    # never dropping below 60% -- carrying extra time is a real but modest
    # cost, not disqualifying.
    if days_after_earnings <= 5:
        return WEIGHT_EVENT_FIT
    excess = min(days_after_earnings - 5, 25)
    decay = Decimal(excess) / Decimal(25) * Decimal("0.4")
    return int(WEIGHT_EVENT_FIT * (1 - decay))


def _dte_suitability_score(dte: int) -> int:
    if _DTE_SWEET_SPOT_LOW <= dte <= _DTE_SWEET_SPOT_HIGH:
        return WEIGHT_DTE_SUITABILITY
    if dte < _DTE_SWEET_SPOT_LOW:
        # Very short DTE: real gamma-risk cost, but not disqualifying.
        fraction = Decimal(dte) / Decimal(_DTE_SWEET_SPOT_LOW)
        return int(WEIGHT_DTE_SUITABILITY * max(Decimal("0.3"), fraction))
    excess = min(dte - _DTE_SWEET_SPOT_HIGH, _DTE_MAX_CONSIDERED - _DTE_SWEET_SPOT_HIGH)
    fraction = 1 - Decimal(excess) / Decimal(_DTE_MAX_CONSIDERED - _DTE_SWEET_SPOT_HIGH)
    return int(WEIGHT_DTE_SUITABILITY * max(Decimal("0"), fraction))


def build_expiration_candidate(
    expiration: date,
    reference_date: date,
    earnings_date: date | None,
    quotes: list[OptionQuote],
    underlying_price: Decimal | None,
) -> ExpirationCandidate:
    """Builds one scored candidate from a real, already-fetched chain for
    ``expiration``. ``quotes`` must all share this expiration -- the caller
    (services/expiration_engine.py) is responsible for fetching one real
    chain per candidate; this function never fabricates or interpolates
    contracts for an expiration it wasn't given quotes for."""
    n = len(quotes)
    dte = (expiration - reference_date).days
    days_after_earnings = (expiration - earnings_date).days if earnings_date is not None else None
    excluded_pre_earnings = earnings_date is not None and expiration <= earnings_date

    if n == 0:
        empty_score = ExpirationScoreBreakdown(0, 0, 0, 0, 0, 0)
        return ExpirationCandidate(
            expiration=expiration,
            dte=dte,
            days_after_earnings=days_after_earnings,
            contract_count=0,
            priceable_contract_count=0,
            quote_coverage=Decimal(0),
            bid_ask_coverage=Decimal(0),
            oi_coverage=Decimal(0),
            volume_coverage=Decimal(0),
            atm_iv=None,
            atm_spread_pct=None,
            quality="untradeable",
            score=empty_score,
            is_earnings_anchored=earnings_date is not None,
            excluded_pre_earnings=excluded_pre_earnings,
        )

    priceable = [
        q for q in quotes if q.bid is not None or q.ask is not None or q.last_price is not None
    ]
    bid_ask = [q for q in quotes if q.bid is not None and q.ask is not None]
    quote_coverage = Decimal(len(priceable)) / Decimal(n)
    bid_ask_coverage = Decimal(len(bid_ask)) / Decimal(n)
    oi_coverage = Decimal(sum(1 for q in quotes if q.open_interest is not None)) / Decimal(n)
    volume_coverage = Decimal(sum(1 for q in quotes if q.volume is not None)) / Decimal(n)
    quality = _classify_quality(float(quote_coverage))

    atm_quotes = _find_atm_quotes(quotes, underlying_price)
    atm_iv = _atm_iv(atm_quotes)
    atm_spread_pct = _atm_spread_pct(atm_quotes)

    qualities_present = {
        q.market_data_quality for q in quotes if q.market_data_quality is not None
    }
    data_quality_fraction = (
        min(
            (_DATA_QUALITY_SCORE.get(q, Decimal("0")) for q in qualities_present),
            default=Decimal("0"),
        )
        if qualities_present
        else Decimal("0")
    )

    score = ExpirationScoreBreakdown(
        event_fit=0 if excluded_pre_earnings else _event_fit_score(dte, days_after_earnings),
        liquidity=int(WEIGHT_LIQUIDITY * bid_ask_coverage),
        quote_coverage=int(WEIGHT_QUOTE_COVERAGE * quote_coverage),
        bid_ask_quality=(
            int(WEIGHT_BID_ASK_QUALITY * max(Decimal("0"), 1 - min(atm_spread_pct, Decimal("1"))))
            if atm_spread_pct is not None
            else int(WEIGHT_BID_ASK_QUALITY * Decimal("0.3"))
        ),
        dte_suitability=_dte_suitability_score(dte),
        data_quality=int(WEIGHT_DATA_QUALITY * data_quality_fraction),
    )

    return ExpirationCandidate(
        expiration=expiration,
        dte=dte,
        days_after_earnings=days_after_earnings,
        contract_count=n,
        priceable_contract_count=len(priceable),
        quote_coverage=quote_coverage,
        bid_ask_coverage=bid_ask_coverage,
        oi_coverage=oi_coverage,
        volume_coverage=volume_coverage,
        atm_iv=atm_iv,
        atm_spread_pct=atm_spread_pct,
        quality=quality,
        score=score,
        is_earnings_anchored=earnings_date is not None,
        excluded_pre_earnings=excluded_pre_earnings,
    )


def select_best_expiration(candidates: list[ExpirationCandidate]) -> ExpirationCandidate | None:
    """Highest total score among candidates that actually cover the
    earnings event (excluded_pre_earnings=False) and have at least one
    priceable contract -- never silently picks an untradeable or
    pre-earnings candidate just because it scored highest among a bad set.
    Ties broken by nearest DTE (least excess time carried)."""
    eligible = [
        c for c in candidates if not c.excluded_pre_earnings and c.priceable_contract_count > 0
    ]
    if not eligible:
        return None
    return max(eligible, key=lambda c: (c.score.total, -c.dte))


def build_expiration_reasons(
    selected: ExpirationCandidate, alternatives: list[ExpirationCandidate]
) -> list[str]:
    """Deterministic "why this expiration" bullets built only from real
    numbers already on the candidates -- never invented, matching the style
    of analytics/decision/reasoning.py."""
    reasons: list[str] = []
    if selected.is_earnings_anchored and selected.days_after_earnings is not None:
        reasons.append(
            f"First liquid expiration considered that falls "
            f"{selected.days_after_earnings} day(s) after the earnings event."
        )
    pct = int(selected.quote_coverage * 100)
    reasons.append(f"{pct}% of contracts at this expiration are priceable ({selected.quality}).")

    better_spread = [
        alt
        for alt in alternatives
        if alt.atm_spread_pct is not None
        and selected.atm_spread_pct is not None
        and alt.atm_spread_pct > selected.atm_spread_pct
    ]
    if better_spread and selected.atm_spread_pct is not None:
        worse = max(better_spread, key=lambda a: a.atm_spread_pct or Decimal(0))
        if worse.atm_spread_pct is not None:
            reasons.append(
                f"ATM bid/ask quality is better than {worse.expiration.isoformat()} "
                f"({float(selected.atm_spread_pct) * 100:.1f}% spread vs. "
                f"{float(worse.atm_spread_pct) * 100:.1f}%)."
            )

    much_later = [alt for alt in alternatives if alt.dte > selected.dte + 14]
    if much_later:
        furthest = max(much_later, key=lambda a: a.dte)
        reasons.append(
            f"{furthest.expiration.isoformat()} ({furthest.dte} DTE) adds "
            f"{furthest.dte - selected.dte} extra days of time premium."
        )

    if selected.atm_iv is not None:
        reasons.append(
            f"ATM implied volatility at this expiration is {float(selected.atm_iv) * 100:.1f}%."
        )

    return reasons


@dataclass(frozen=True)
class ExpirationSelectionResult:
    mode: Literal["auto", "manual"]
    selected: ExpirationCandidate | None
    alternatives: list[ExpirationCandidate] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    warning: str | None = None
