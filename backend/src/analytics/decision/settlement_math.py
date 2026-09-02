"""Phase 4.5 -- realized P&L, return %, R-multiple, and win/loss for a
closed benchmark position. Pure arithmetic on two already-captured
prices per leg (entry, from EntrySnapshot; exit, from ExitSnapshot) --
deliberately not folded into analytics/options/payoff.py (a different,
pre-trade payoff-diagram concern: max profit/loss, breakevens from a
single premium) or analytics/decision/budget.py (a different, pre-trade
sizing concern: how many contracts fit the budget). Sizing --
EntrySnapshot.quantity (the per-leg ratio within one "set" of the
strategy, e.g. 1/2/1 for a butterfly) and EntryCaptureAttempt.contracts
(how many sets were actually bought) -- is never re-derived here; both
are frozen at entry and simply read (Phase 4.5 approved decision 3: "Do
not recalculate sizing during settlement").
"""

from dataclasses import dataclass
from decimal import Decimal

from analytics.decision.budget import CONTRACT_MULTIPLIER
from models.enums import OptionAction

_DIRECTION_SIGN: dict[OptionAction, int] = {
    OptionAction.BUY: 1,
    OptionAction.SELL: -1,
}


def leg_realized_pnl_per_share(
    action: OptionAction, entry_price: Decimal, exit_price: Decimal
) -> Decimal:
    """(exit - entry) * direction_sign -- the entire per-leg P&L formula,
    unscaled by quantity/multiplier/contracts. BUY (+1): gain if the
    contract appreciated (bought at the entry ask, sold at the exit
    bid). SELL (-1): gain if it decayed (opened as a credit at the entry
    bid, closed by paying the exit ask)."""
    sign = _DIRECTION_SIGN[action]
    return (exit_price - entry_price) * sign


def leg_exit_signed_value_per_ratio_share(
    action: OptionAction, exit_price: Decimal, quantity_ratio: int
) -> Decimal:
    """The exit side's own equivalent of
    analytics/options/payoff.py::OptionLeg.signed_premium, for the
    *closing* transaction -- positive = debit paid to close, negative =
    credit received closing. Closing a leg is always the opposite
    transaction of opening it (a long/BUY leg is closed by selling, a
    short/SELL leg is closed by buying), hence the sign flip relative to
    signed_premium's own entry-side convention (which signs by the
    *opening* action)."""
    sign = _DIRECTION_SIGN[action]
    return -sign * exit_price * quantity_ratio


@dataclass(frozen=True)
class SettlementLegInput:
    """One leg's already-captured entry and exit fills -- everything
    settlement math needs for that leg, nothing it recomputes."""

    action: OptionAction
    entry_price: Decimal
    exit_price: Decimal
    quantity: int  # the per-leg ratio, e.g. EntrySnapshot.quantity -- not the overall position size
    multiplier: Decimal


@dataclass(frozen=True)
class SettlementTotals:
    """Attempt-level aggregate outcome. ``net_exit_cash`` is an
    independent, honest snapshot of what closing the position actually
    cost/paid at the captured exit prices -- it does not algebraically
    reduce to ``realized_pnl`` by subtracting from ``net_entry_cash``,
    since conservative bid/ask crossing on both legs of a round trip
    means "cash in minus cash out" and "true realized P&L" are not the
    same number by construction (this is expected and correct, not a
    bug -- realized_pnl is always computed directly from each leg's own
    entry/exit fill, never derived from net_entry_cash/net_exit_cash
    arithmetic).
    """

    realized_pnl: Decimal
    net_exit_price_per_share: Decimal
    net_exit_cash: Decimal
    return_pct: Decimal | None
    r_multiple: Decimal | None
    is_win: bool


def compute_settlement_totals(
    legs: list[SettlementLegInput],
    *,
    contracts: int,
    net_entry_cash: Decimal,
    initial_max_risk: Decimal | None,
) -> SettlementTotals:
    """Aggregates every leg's realized P&L into the attempt-level totals.
    ``contracts`` (the overall number of strategy "sets" bought, from
    EntryCaptureAttempt.contracts) scales every leg's per-ratio-share
    figure up to the real total -- the same scaling
    analytics/decision/budget.py::compute_budget_fit already applies to
    go from ``net_premium`` (per ratio-share) to ``total_net_premium``
    (the real dollar ``net_entry_cash``), reused here rather than
    re-derived. ``net_entry_cash``/``initial_max_risk`` are read from the
    linked EntryCaptureAttempt, never recomputed (Phase 4.5 approved
    decision 3).

    Phase 4.5 approved decision 2: ``return_pct = realized_pnl /
    net_entry_cash`` -- the exact same signed, already-computed quantity
    ``net_entry_cash`` already is (ask*quantity*multiplier for a long
    leg, bid*quantity*multiplier received for a short leg, aggregated).
    ``None`` only when that denominator is exactly zero (division by
    zero), never silently reinterpreted or clamped.

    Phase 4.5 approved decision 3: ``r_multiple = realized_pnl /
    initial_max_risk`` -- the existing, already-computed risk-defined
    capital unit. ``None`` when ``initial_max_risk`` is unavailable or
    exactly zero.
    """
    realized_pnl = Decimal(0)
    net_exit_price_per_share = Decimal(0)
    for leg in legs:
        per_share = leg_realized_pnl_per_share(leg.action, leg.entry_price, leg.exit_price)
        realized_pnl += per_share * leg.quantity * leg.multiplier * contracts
        net_exit_price_per_share += leg_exit_signed_value_per_ratio_share(
            leg.action, leg.exit_price, leg.quantity
        )
    net_exit_cash = net_exit_price_per_share * CONTRACT_MULTIPLIER * contracts

    return_pct = (realized_pnl / net_entry_cash) * 100 if net_entry_cash != 0 else None
    r_multiple = (
        realized_pnl / initial_max_risk
        if initial_max_risk is not None and initial_max_risk != 0
        else None
    )

    return SettlementTotals(
        realized_pnl=realized_pnl,
        net_exit_price_per_share=net_exit_price_per_share,
        net_exit_cash=net_exit_cash,
        return_pct=return_pct,
        r_multiple=r_multiple,
        is_win=realized_pnl > 0,
    )
