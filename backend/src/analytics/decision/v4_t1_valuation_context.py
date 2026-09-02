"""Options Decision Engine V4.4A -- T+1 Valuation Context (2026-09-03).

The one typed, immutable input contract every V4.4A scenario-valuation
function reads from. Answers "what do we actually, honestly know about
this candidate at decision time" -- nothing here is ever fabricated
when absent (this task's own Section 4: "Do not fabricate absent
values").

ENTRY COST USES EXECUTABLE SIDES ONLY (Section 5): a long/buy leg's
entry cost is its real ask; a short/sell leg's entry cost is its real
bid. ``V4T1LegInput.entry_executable_price`` is the ONE authoritative
entry-cost accessor every downstream V4.4A function uses -- never a
midpoint (a midpoint is available separately, informational only, via
``entry_mid_price``).

STRATEGY SEMANTICS ARE CARRIED FOR COMPLETENESS ONLY (Section 30):
``strategy_semantics``/``semantic_compatibility`` live on this context
so a caller has everything about one candidate in one place, but
analytics/decision/v4_t1_pricing.py's own repricing functions never
read them -- pricing answers "what happens economically," semantics
answers "does this express the view," and V4.4B is the only place
those two are ever combined.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Literal

from analytics.decision.v4_compatibility import SemanticCompatibilityResult
from analytics.decision.v4_expected_move import ExpectedMoveContext
from analytics.decision.v4_strategy_semantics import StrategySemantics
from analytics.decision.v4_strike_resolver import Right
from analytics.options.strategy_candidates import StrategyCategory

T1_VALUATION_CONTEXT_VERSION = "t1_valuation_context_v1"

# The real DTE floor already established in services/options_reconstruction
# .py::recompute_deterministic_greeks (``max(tte_days, 1)``) -- Black-
# Scholes requires strictly positive time-to-expiry (see analytics/
# options/black_scholes.py::_d1_d2), so a same-day DTE is floored at 1
# calendar day rather than raising or fabricating a fractional value.
MIN_DTE_FOR_PRICING = 1


@dataclass(frozen=True)
class V4T1LegInput:
    """One leg's real, point-in-time market inputs -- decision-time
    only, never a settlement/outcome field."""

    leg_index: int
    action: Literal["buy", "sell"]
    right: Right
    strike: Decimal
    quantity: int
    multiplier: Decimal
    entry_bid: Decimal | None
    entry_ask: Decimal | None
    entry_last: Decimal | None
    entry_iv: Decimal | None
    entry_delta: Decimal | None
    entry_gamma: Decimal | None
    entry_theta: Decimal | None
    entry_vega: Decimal | None
    market_data_quality: str | None
    external_contract_id: str | None

    @property
    def entry_executable_price(self) -> Decimal | None:
        """Section 5 -- the ONE authoritative entry-cost rule: BUY uses
        ASK, SELL uses BID. Never a midpoint."""
        return self.entry_ask if self.action == "buy" else self.entry_bid

    @property
    def entry_mid_price(self) -> Decimal | None:
        """Informational only (Section 5: "midpoint may be shown only
        as an informational diagnostic") -- never used as an official
        entry cost anywhere in V4.4A."""
        if self.entry_bid is not None and self.entry_ask is not None:
            return (self.entry_bid + self.entry_ask) / 2
        return None


@dataclass(frozen=True)
class V4T1ValuationContext:
    # --- MARKET AT DECISION ---
    ticker: str
    underlying_price: Decimal
    observed_at: datetime
    entry_timestamp: datetime
    expected_exit_timestamp: datetime

    # --- OPTION MARKET ---
    strategy: StrategyCategory
    expiration: date
    legs: tuple[V4T1LegInput, ...]

    # --- EXPECTED MOVE (reuses V4.3's own object directly -- never a
    # second, parallel expected-move computation) ---
    expected_move_context: ExpectedMoveContext

    # --- STRATEGY (informational only -- Section 30) ---
    strategy_semantics: StrategySemantics | None = None
    semantic_compatibility: SemanticCompatibilityResult | None = None
    geometry_variant_id: str | None = None

    market_data_quality_note: str | None = None
    context_version: str = T1_VALUATION_CONTEXT_VERSION

    @property
    def holding_period(self) -> timedelta:
        return self.expected_exit_timestamp - self.entry_timestamp

    @property
    def dte_entry(self) -> int:
        """Real calendar days remaining at entry -- NOT floored; a
        genuinely negative or zero value here is a real data problem
        the caller should see, not silently hidden."""
        return (self.expiration - self.entry_timestamp.date()).days

    @property
    def dte_exit(self) -> int:
        return (self.expiration - self.expected_exit_timestamp.date()).days

    def dte_exit_for_pricing(self) -> int:
        """Section 2's own concern made concrete: never accidentally
        value at expiration. Floors at ``MIN_DTE_FOR_PRICING`` (matching
        the project's own established convention) so Black-Scholes
        always receives strictly positive time-to-expiry, while
        ``dte_exit`` itself stays unfloored for honest diagnostics."""
        return max(self.dte_exit, MIN_DTE_FOR_PRICING)
