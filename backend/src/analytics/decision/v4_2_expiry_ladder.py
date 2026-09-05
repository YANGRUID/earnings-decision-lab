"""V4.2 CHALLENGER -- a bounded expiry ladder and its settlement-day risk.

V4.1 picks an expiration with ``select_expiration_after``: the nearest listed
expiry strictly after the earnings date. That is an index, not a comparison --
no economics, no liquidity, and no awareness of when the position is actually
settled. Over the first seven natural events it selected an expiry that
expired ON the T+1 settlement day five times, which is where the 2026-09-04
empty-book incident came from: at 15:30 on expiry day the deep-OTM wings had
no bid left to read.

This module does NOT ban short-dated expiries. A same-day expiry can be the
right instrument, and the audit was explicit that the answer is to COMPARE
expiries on the same T+1 objective rather than to legislate a minimum DTE.
What it provides is the bounded set of alternatives to compare, and an
explicit statement of the settlement-day risk each one carries, so the
comparison can happen on evidence.

Scope, stated plainly: this is foundation. It selects and characterises
expiry variants and is exercised by tests and the diagnostic replay. It is
deliberately NOT wired into candidate generation -- doing that half-way would
ship an official behaviour change under a challenger flag, which this phase
forbids.
"""

from dataclasses import dataclass
from datetime import date

EXPIRY_LADDER_VERSION = "v4_2_expiry_ladder_v1"

#: How many listed expiries past the earnings date the ladder will consider.
#: Three is a deliberate bound: it spans the same-day/near weekly, the
#: following weekly and the next monthly for a typical US equity chain, while
#: keeping targeted contract resolution to a size that cannot become a chain
#: sweep.
DEFAULT_MAX_VARIANTS = 3

#: Settlement-day risk classes, ordered by how exposed the exit is to an
#: empty book at the 15:30 settlement instant.
RISK_EXPIRES_ON_SETTLEMENT = "EXPIRES_ON_SETTLEMENT_DATE"
RISK_EXPIRES_SAME_WEEK = "EXPIRES_WITHIN_A_WEEK_OF_SETTLEMENT"
RISK_STANDARD = "STANDARD_REMAINING_LIFE"


@dataclass(frozen=True)
class ExpiryVariant:
    """One candidate expiration, characterised against the ACTUAL settlement
    instant rather than against the earnings date alone."""

    expiration: date
    ladder_position: int  # 0 = nearest listed expiry after the earnings date
    entry_dte: int  # calendar days from the decision date to expiry
    dte_at_settlement: int  # calendar days remaining when the position is closed
    settlement_risk: str
    version: str = EXPIRY_LADDER_VERSION

    @property
    def expires_on_settlement_date(self) -> bool:
        return self.dte_at_settlement == 0

    @property
    def has_remaining_life_at_exit(self) -> bool:
        """Whether the position still holds extrinsic value at the moment it
        is settled -- the property that gives the exit a two-sided market."""
        return self.dte_at_settlement > 0


def settlement_date_for(earnings_date: date, trading_days: list[date]) -> date | None:
    """The first trading session after ``earnings_date`` -- the session V4
    settles in for an AMC event.

    ``trading_days`` must be real session dates the caller already holds; this
    module never invents a calendar or assumes a weekday is open.
    """
    later = sorted(d for d in trading_days if d > earnings_date)
    return later[0] if later else None


def classify_settlement_risk(expiration: date, settlement_date: date) -> str:
    delta = (expiration - settlement_date).days
    if delta <= 0:
        return RISK_EXPIRES_ON_SETTLEMENT
    if delta <= 7:
        return RISK_EXPIRES_SAME_WEEK
    return RISK_STANDARD


def build_expiry_ladder(
    available_expirations: set[date],
    *,
    earnings_date: date,
    settlement_date: date,
    decision_date: date,
    max_variants: int = DEFAULT_MAX_VARIANTS,
) -> list[ExpiryVariant]:
    """The bounded set of expiries worth comparing for one event.

    Keeps V4.1's eligibility rule verbatim -- an expiration must be strictly
    after the earnings date, or the event is not inside its remaining life --
    and then, instead of taking the first one, returns the nearest
    ``max_variants`` of them for comparison on the same T+1 objective.
    """
    eligible = sorted(exp for exp in available_expirations if exp > earnings_date)
    return [
        ExpiryVariant(
            expiration=expiration,
            ladder_position=position,
            entry_dte=(expiration - decision_date).days,
            dte_at_settlement=(expiration - settlement_date).days,
            settlement_risk=classify_settlement_risk(expiration, settlement_date),
        )
        for position, expiration in enumerate(eligible[: max(0, max_variants)])
    ]


def v4_1_selection(available_expirations: set[date], earnings_date: date) -> date | None:
    """Exactly what V4.1 would pick, for side-by-side comparison. Mirrors
    ``analytics/options/implied_move.select_expiration_after`` rather than
    importing it, because this module must keep working if that rule ever
    changes underneath the comparison."""
    eligible = [exp for exp in available_expirations if exp > earnings_date]
    return min(eligible) if eligible else None


@dataclass(frozen=True)
class LiquidityObservation:
    """Real, observed execution evidence for one expiry variant. Every field
    is a measurement or None -- never a default standing in for one."""

    expiration: date
    mean_relative_spread: object | None = None
    worst_relative_spread: object | None = None
    min_bid_size: int | None = None
    min_ask_size: int | None = None
    legs_with_empty_bid: int = 0
    legs_observed: int = 0

    @property
    def empty_bid_rate(self) -> float | None:
        """Fraction of the observed legs that had no bid at all -- the direct
        precursor of a settlement that cannot be priced on its required side.
        None when nothing was observed, never a reassuring zero."""
        if not self.legs_observed:
            return None
        return self.legs_with_empty_bid / self.legs_observed


def compare_expiry_variants(
    variants: list[ExpiryVariant],
    liquidity: dict[date, LiquidityObservation] | None = None,
) -> list[dict]:
    """A diagnostic view: every variant with its settlement-day risk and,
    where it was actually observed, its execution quality.

    Deliberately returns diagnostics rather than a winner. Choosing between
    expiries requires each variant's own modeled T+1 economics, which needs
    candidate construction per expiry -- the part this phase does not ship.
    """
    liquidity = liquidity or {}
    out: list[dict] = []
    for variant in variants:
        observed = liquidity.get(variant.expiration)
        out.append(
            {
                "expiration": variant.expiration.isoformat(),
                "ladder_position": variant.ladder_position,
                "entry_dte": variant.entry_dte,
                "dte_at_settlement": variant.dte_at_settlement,
                "settlement_risk": variant.settlement_risk,
                "expires_on_settlement_date": variant.expires_on_settlement_date,
                "has_remaining_life_at_exit": variant.has_remaining_life_at_exit,
                "mean_relative_spread": (
                    None if observed is None else observed.mean_relative_spread
                ),
                "empty_bid_legs": None if observed is None else observed.legs_with_empty_bid,
                "legs_observed": None if observed is None else observed.legs_observed,
            }
        )
    return out
