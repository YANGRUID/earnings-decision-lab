"""Deterministic price-move calculations. No LLM involvement — every value
here is arithmetic on stored ``PriceBar`` rows, per the project rule that
financial calculations are never delegated to a model.
"""

from datetime import date
from decimal import Decimal


class InsufficientPriceHistory(Exception):
    """Raised when a calculation needs a bar that isn't in the provided series."""


def pct_change(start: Decimal, end: Decimal) -> Decimal:
    if start == 0:
        raise ValueError("cannot compute percent change from a zero base price")
    return (end - start) / start


def bars_as_of_or_before(bars: dict[date, Decimal], as_of: date) -> date | None:
    """Latest trade date <= ``as_of`` present in ``bars``, or None."""
    candidates = [d for d in bars if d <= as_of]
    return max(candidates) if candidates else None


def nth_trading_day_close(
    bars: dict[date, Decimal], anchor: date, n: int
) -> tuple[date, Decimal]:
    """The close ``n`` trading days after (n>0) or before (n<0) ``anchor``.

    ``bars`` must be a {trade_date: close} map. ``anchor`` itself need not be
    a trading day (e.g. an earnings date that falls after market close).
    """
    trading_days = sorted(bars)
    if n >= 0:
        eligible = [d for d in trading_days if d > anchor]
        index = n - 1 if n > 0 else -1
        if n == 0:
            eligible = [d for d in trading_days if d <= anchor]
            if not eligible:
                raise InsufficientPriceHistory(f"no trading day on/before {anchor}")
            target = eligible[-1]
            return target, bars[target]
        if len(eligible) <= index:
            raise InsufficientPriceHistory(f"fewer than {n} trading days after {anchor}")
        target = eligible[index]
        return target, bars[target]
    else:
        eligible = [d for d in trading_days if d <= anchor]
        index = len(eligible) + n  # n is negative
        if index < 0:
            raise InsufficientPriceHistory(f"fewer than {-n} trading days before {anchor}")
        target = eligible[index]
        return target, bars[target]


def recent_return(bars: dict[date, Decimal], as_of: date, lookback_trading_days: int) -> Decimal:
    """Percent return over the ``lookback_trading_days`` trading days ending
    at (or just before) ``as_of``. Uses only bars with trade_date <= as_of —
    the no-lookahead-bias invariant for this calculation.
    """
    end_date = bars_as_of_or_before(bars, as_of)
    if end_date is None:
        raise InsufficientPriceHistory(f"no trading day on/before {as_of}")
    eligible = sorted(d for d in bars if d <= as_of)
    index = len(eligible) - 1 - lookback_trading_days
    if index < 0:
        raise InsufficientPriceHistory(
            f"fewer than {lookback_trading_days} trading days before {as_of}"
        )
    start_date = eligible[index]
    return pct_change(bars[start_date], bars[end_date])


def price_reaction_moves(
    bars: dict[date, Decimal], earnings_date: date
) -> dict[str, tuple[date, Decimal] | None]:
    """Close-price-before / next-day-close / five-day-close and their move
    percentages relative to the last close before ``earnings_date``.

    Returns a dict with keys close_price_before, next_day_close,
    five_day_close (each a (date, price) tuple or None if unavailable) plus
    next_day_move_pct / five_day_move_pct (Decimal or None).
    """
    result: dict[str, tuple[date, Decimal] | None] = {
        "close_price_before": None,
        "next_day_close": None,
        "five_day_close": None,
    }
    moves: dict[str, Decimal | None] = {"next_day_move_pct": None, "five_day_move_pct": None}

    try:
        result["close_price_before"] = nth_trading_day_close(bars, earnings_date, 0)
    except InsufficientPriceHistory:
        return {**result, **moves}

    before_price = result["close_price_before"][1]

    try:
        result["next_day_close"] = nth_trading_day_close(bars, earnings_date, 1)
        moves["next_day_move_pct"] = pct_change(before_price, result["next_day_close"][1])
    except InsufficientPriceHistory:
        pass

    try:
        result["five_day_close"] = nth_trading_day_close(bars, earnings_date, 5)
        moves["five_day_move_pct"] = pct_change(before_price, result["five_day_close"][1])
    except InsufficientPriceHistory:
        pass

    return {**result, **moves}
