from datetime import date
from decimal import Decimal

import pytest

from analytics.earnings.price_moves import (
    InsufficientPriceHistory,
    bars_as_of_or_before,
    nth_trading_day_close,
    pct_change,
    price_reaction_moves,
    recent_return,
)

# A run of 10 consecutive trading days (Mon-Fri x2), Sep 8-19 2025, closes
# 100, 101, ..., 109. Sep 13/14 and 20/21 (weekends) are intentionally
# excluded to model real trading-day gaps.
BARS = {
    date(2025, 9, 8): Decimal("100"),
    date(2025, 9, 9): Decimal("101"),
    date(2025, 9, 10): Decimal("102"),
    date(2025, 9, 11): Decimal("103"),
    date(2025, 9, 12): Decimal("104"),
    date(2025, 9, 15): Decimal("105"),
    date(2025, 9, 16): Decimal("106"),
    date(2025, 9, 17): Decimal("107"),
    date(2025, 9, 18): Decimal("108"),
    date(2025, 9, 19): Decimal("109"),
}


def test_pct_change():
    assert pct_change(Decimal("100"), Decimal("110")) == Decimal("0.10")
    assert pct_change(Decimal("100"), Decimal("90")) == Decimal("-0.10")


def test_pct_change_zero_base_raises():
    with pytest.raises(ValueError):
        pct_change(Decimal("0"), Decimal("10"))


def test_bars_as_of_or_before_exact_and_weekend():
    assert bars_as_of_or_before(BARS, date(2025, 9, 10)) == date(2025, 9, 10)
    # Sept 13 is a Saturday with no bar — falls back to the prior Friday.
    assert bars_as_of_or_before(BARS, date(2025, 9, 13)) == date(2025, 9, 12)


def test_bars_as_of_or_before_no_data_returns_none():
    assert bars_as_of_or_before(BARS, date(2025, 1, 1)) is None


def test_nth_trading_day_close_zero_is_last_close_on_or_before():
    trade_date, price = nth_trading_day_close(BARS, date(2025, 9, 13), 0)
    assert trade_date == date(2025, 9, 12)
    assert price == Decimal("104")


def test_nth_trading_day_close_next_day_skips_weekend():
    # Anchor on Friday the 12th; "next trading day" should be Monday the 15th.
    trade_date, price = nth_trading_day_close(BARS, date(2025, 9, 12), 1)
    assert trade_date == date(2025, 9, 15)
    assert price == Decimal("105")


def test_nth_trading_day_close_five_days_after():
    trade_date, price = nth_trading_day_close(BARS, date(2025, 9, 10), 5)
    assert trade_date == date(2025, 9, 17)
    assert price == Decimal("107")


def test_nth_trading_day_close_insufficient_history_raises():
    with pytest.raises(InsufficientPriceHistory):
        nth_trading_day_close(BARS, date(2025, 9, 18), 5)


def test_recent_return_uses_only_data_on_or_before_as_of():
    # 5-trading-day return ending 9/12: (104 - 99... ) — start index = end - 5.
    ret = recent_return(BARS, date(2025, 9, 12), lookback_trading_days=4)
    # eligible dates on/before 9/12: 9/8..9/12 (5 days), index = 5-1-4 = 0 -> 9/8 (100)
    assert ret == pct_change(Decimal("100"), Decimal("104"))


def test_recent_return_insufficient_history_raises():
    with pytest.raises(InsufficientPriceHistory):
        recent_return(BARS, date(2025, 9, 9), lookback_trading_days=20)


def test_recent_return_never_uses_future_bars():
    # A future bar beyond as_of must not affect the calculation at all.
    bars_with_future = {**BARS, date(2025, 9, 22): Decimal("999")}
    ret_without_future = recent_return(BARS, date(2025, 9, 12), lookback_trading_days=4)
    ret_with_future = recent_return(bars_with_future, date(2025, 9, 12), lookback_trading_days=4)
    assert ret_without_future == ret_with_future


def test_price_reaction_moves_full_window():
    moves = price_reaction_moves(BARS, earnings_date=date(2025, 9, 10))
    assert moves["close_price_before"] == (date(2025, 9, 10), Decimal("102"))
    assert moves["next_day_close"] == (date(2025, 9, 11), Decimal("103"))
    assert moves["five_day_close"] == (date(2025, 9, 17), Decimal("107"))
    assert moves["next_day_move_pct"] == pct_change(Decimal("102"), Decimal("103"))
    assert moves["five_day_move_pct"] == pct_change(Decimal("102"), Decimal("107"))


def test_price_reaction_moves_partial_window_returns_none_for_missing():
    # Earnings date near the end of available history: five_day_close unavailable.
    moves = price_reaction_moves(BARS, earnings_date=date(2025, 9, 18))
    assert moves["close_price_before"] == (date(2025, 9, 18), Decimal("108"))
    assert moves["next_day_close"] == (date(2025, 9, 19), Decimal("109"))
    assert moves["five_day_close"] is None
    assert moves["five_day_move_pct"] is None


def test_price_reaction_moves_no_data_before_earnings_returns_all_none():
    moves = price_reaction_moves(BARS, earnings_date=date(2020, 1, 1))
    assert moves["close_price_before"] is None
    assert moves["next_day_close"] is None
    assert moves["five_day_close"] is None
