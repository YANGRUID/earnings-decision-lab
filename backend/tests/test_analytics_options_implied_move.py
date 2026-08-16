from datetime import date, datetime
from decimal import Decimal

import pytest

from analytics.options.implied_move import (
    NoQuoteAvailable,
    calculate_atm_straddle_implied_move,
    find_atm_strike,
    mid_price,
)
from providers.types import OptionQuote

NOW = datetime(2025, 9, 22, 20, 0, 0)
EXP = date(2025, 9, 26)


def _quote(strike, option_type, bid=None, ask=None, last=None) -> OptionQuote:
    return OptionQuote(
        ticker="MU",
        snapshot_timestamp=NOW,
        expiration_date=EXP,
        strike=Decimal(str(strike)),
        option_type=option_type,
        bid=Decimal(str(bid)) if bid is not None else None,
        ask=Decimal(str(ask)) if ask is not None else None,
        last_price=Decimal(str(last)) if last is not None else None,
        source_provider="test",
        retrieved_at=NOW,
    )


def test_mid_price_uses_bid_ask_midpoint():
    q = _quote(115, "call", bid=4.2, ask=4.4)
    assert mid_price(q) == Decimal("4.3")


def test_mid_price_falls_back_to_last_price():
    q = _quote(115, "call", last=4.35)
    assert mid_price(q) == Decimal("4.35")


def test_mid_price_raises_without_any_price():
    q = _quote(115, "call")
    with pytest.raises(NoQuoteAvailable):
        mid_price(q)


def test_find_atm_strike_picks_nearest():
    quotes = [_quote(k, "call") for k in (100, 110, 115, 120, 130)]
    assert find_atm_strike(quotes, Decimal("116")) == Decimal("115")


def test_calculate_atm_straddle_implied_move():
    quotes = [
        _quote(115, "call", bid=4.20, ask=4.40),
        _quote(115, "put", bid=4.00, ask=4.20),
        _quote(110, "call", bid=6.00, ask=6.20),  # not ATM — should be ignored
    ]
    result = calculate_atm_straddle_implied_move(
        quotes, expiration=EXP, underlying_price=Decimal("114.50"), computed_at=NOW
    )

    assert result.method == "atm_straddle"
    assert result.atm_strike == Decimal("115")
    assert result.call_mid == Decimal("4.30")
    assert result.put_mid == Decimal("4.10")
    assert result.implied_move_absolute == Decimal("8.40")
    assert result.implied_move_pct == Decimal("8.40") / Decimal("114.50")
    assert result.as_inputs_json()["method"] == "atm_straddle"


def test_calculate_atm_straddle_raises_for_missing_expiration():
    quotes = [_quote(115, "call", bid=4.2, ask=4.4), _quote(115, "put", bid=4.0, ask=4.2)]
    with pytest.raises(NoQuoteAvailable):
        calculate_atm_straddle_implied_move(
            quotes, expiration=date(2099, 1, 1), underlying_price=Decimal("115"), computed_at=NOW
        )


def test_calculate_atm_straddle_raises_when_put_missing_at_atm_strike():
    quotes = [_quote(115, "call", bid=4.2, ask=4.4)]
    with pytest.raises(NoQuoteAvailable):
        calculate_atm_straddle_implied_move(
            quotes, expiration=EXP, underlying_price=Decimal("115"), computed_at=NOW
        )
