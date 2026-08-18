from datetime import date, datetime
from decimal import Decimal

import pytest

from analytics.options.implied_move import (
    ATM_IV_METHOD_AVERAGE,
    ATM_IV_METHOD_SINGLE_SIDE,
    NoQuoteAvailable,
    calculate_atm_iv,
    calculate_atm_straddle_implied_move,
    find_atm_strike,
    mid_price,
    select_expiration_after,
    select_nearest_listed_expiration,
    select_target_expiration_and_anchor,
)
from models.enums import OptionsSnapshotAnchor
from providers.types import OptionQuote

NOW = datetime(2025, 9, 22, 20, 0, 0)
EXP = date(2025, 9, 26)


def _quote(strike, option_type, bid=None, ask=None, last=None, iv=None) -> OptionQuote:
    return OptionQuote(
        ticker="MU",
        snapshot_timestamp=NOW,
        expiration_date=EXP,
        strike=Decimal(str(strike)),
        option_type=option_type,
        bid=Decimal(str(bid)) if bid is not None else None,
        ask=Decimal(str(ask)) if ask is not None else None,
        last_price=Decimal(str(last)) if last is not None else None,
        implied_volatility=Decimal(str(iv)) if iv is not None else None,
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


def test_select_expiration_after_picks_nearest_strictly_after_earnings_date():
    expirations = {date(2025, 9, 19), date(2025, 9, 26), date(2025, 10, 3)}
    assert select_expiration_after(expirations, date(2025, 9, 22)) == date(2025, 9, 26)


def test_select_expiration_after_excludes_expiration_on_earnings_date():
    expirations = {date(2025, 9, 22), date(2025, 9, 26)}
    assert select_expiration_after(expirations, date(2025, 9, 22)) == date(2025, 9, 26)


def test_select_expiration_after_returns_none_when_all_expirations_are_before():
    expirations = {date(2025, 9, 1), date(2025, 9, 10)}
    assert select_expiration_after(expirations, date(2025, 9, 22)) is None


def test_select_expiration_after_returns_none_for_empty_set():
    assert select_expiration_after(set(), date(2025, 9, 22)) is None


def test_select_nearest_listed_expiration_allows_same_day():
    # Unlike select_expiration_after, a general/current snapshot has no
    # earnings event a same-day expiration would need to outlive.
    expirations = {date(2025, 9, 22), date(2025, 9, 26)}
    assert select_nearest_listed_expiration(expirations, date(2025, 9, 22)) == date(2025, 9, 22)


def test_select_nearest_listed_expiration_picks_nearest_after_reference():
    expirations = {date(2025, 9, 19), date(2025, 9, 26), date(2025, 10, 3)}
    assert select_nearest_listed_expiration(expirations, date(2025, 9, 22)) == date(2025, 9, 26)


def test_select_nearest_listed_expiration_returns_none_when_all_before():
    expirations = {date(2025, 9, 1), date(2025, 9, 10)}
    assert select_nearest_listed_expiration(expirations, date(2025, 9, 22)) is None


def test_select_target_expiration_and_anchor_earnings_anchored_when_date_known():
    expirations = {date(2025, 9, 19), date(2025, 9, 26)}
    expiration, anchor = select_target_expiration_and_anchor(
        expirations, date(2025, 9, 22), date(2025, 9, 15)
    )
    assert expiration == date(2025, 9, 26)
    assert anchor == OptionsSnapshotAnchor.EARNINGS_ANCHORED


def test_select_target_expiration_and_anchor_general_when_date_unknown():
    expirations = {date(2025, 9, 19), date(2025, 9, 26)}
    # reference_date (the snapshot's own date) allows the same-day 9/19
    # expiration -- general mode, no earnings event to outlive.
    expiration, anchor = select_target_expiration_and_anchor(
        expirations, None, date(2025, 9, 19)
    )
    assert expiration == date(2025, 9, 19)
    assert anchor == OptionsSnapshotAnchor.GENERAL_CURRENT


def test_calculate_atm_iv_averages_call_and_put():
    call = _quote(115, "call", iv=0.50)
    put = _quote(115, "put", iv=0.52)
    result = calculate_atm_iv(call, put)

    assert result.method == ATM_IV_METHOD_AVERAGE
    assert result.atm_iv == Decimal("0.51")
    assert result.diverges is False


def test_calculate_atm_iv_flags_divergence_above_threshold():
    call = _quote(115, "call", iv=0.40)
    put = _quote(115, "put", iv=0.50)
    result = calculate_atm_iv(call, put)

    assert result.method == ATM_IV_METHOD_AVERAGE
    assert result.diverges is True


def test_calculate_atm_iv_uses_single_side_when_call_iv_missing():
    call = _quote(115, "call")
    put = _quote(115, "put", iv=0.52)
    result = calculate_atm_iv(call, put)

    assert result.method == ATM_IV_METHOD_SINGLE_SIDE
    assert result.atm_iv == Decimal("0.52")
    assert result.diverges is False


def test_calculate_atm_iv_uses_single_side_when_put_iv_missing():
    call = _quote(115, "call", iv=0.48)
    put = _quote(115, "put")
    result = calculate_atm_iv(call, put)

    assert result.method == ATM_IV_METHOD_SINGLE_SIDE
    assert result.atm_iv == Decimal("0.48")


def test_calculate_atm_iv_returns_none_when_neither_side_has_iv():
    call = _quote(115, "call")
    put = _quote(115, "put")
    result = calculate_atm_iv(call, put)

    assert result.method == ATM_IV_METHOD_SINGLE_SIDE
    assert result.atm_iv is None
