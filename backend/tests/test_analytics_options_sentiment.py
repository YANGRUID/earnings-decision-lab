from datetime import date, datetime
from decimal import Decimal

from analytics.options.sentiment import (
    atm_iv_at_expiration,
    iv_term_structure,
    put_call_open_interest_ratio,
    put_call_volume_ratio,
)
from providers.types import OptionQuote

NOW = datetime(2025, 9, 22, 20, 0, 0)
NEAR_EXP = date(2025, 9, 26)
NEXT_EXP = date(2025, 10, 17)


def _quote(
    strike,
    option_type,
    expiration=NEAR_EXP,
    oi=None,
    volume=None,
    iv=None,
) -> OptionQuote:
    return OptionQuote(
        ticker="MU",
        snapshot_timestamp=NOW,
        expiration_date=expiration,
        strike=Decimal(str(strike)),
        option_type=option_type,
        open_interest=oi,
        volume=volume,
        implied_volatility=Decimal(str(iv)) if iv is not None else None,
        source_provider="test",
        retrieved_at=NOW,
    )


def test_put_call_open_interest_ratio():
    quotes = [
        _quote(115, "call", oi=1000),
        _quote(120, "call", oi=500),
        _quote(115, "put", oi=600),
        _quote(120, "put", oi=900),
    ]
    assert put_call_open_interest_ratio(quotes) == Decimal("1500") / Decimal("1500")


def test_put_call_open_interest_ratio_none_when_no_call_open_interest():
    quotes = [_quote(115, "call", oi=None), _quote(115, "put", oi=500)]
    assert put_call_open_interest_ratio(quotes) is None


def test_put_call_open_interest_ratio_none_with_no_quotes():
    assert put_call_open_interest_ratio([]) is None


def test_put_call_volume_ratio():
    quotes = [
        _quote(115, "call", volume=400),
        _quote(115, "put", volume=200),
    ]
    assert put_call_volume_ratio(quotes) == Decimal("0.5")


def test_atm_iv_at_expiration_returns_none_without_expiration_match():
    quotes = [_quote(115, "call", expiration=NEAR_EXP, iv=0.5)]
    assert atm_iv_at_expiration(quotes, date(2099, 1, 1), Decimal("115")) is None


def test_atm_iv_at_expiration_returns_none_when_put_missing():
    quotes = [_quote(115, "call", expiration=NEAR_EXP, iv=0.5)]
    assert atm_iv_at_expiration(quotes, NEAR_EXP, Decimal("115")) is None


def test_atm_iv_at_expiration_computes_average():
    quotes = [
        _quote(115, "call", expiration=NEAR_EXP, iv=0.50),
        _quote(115, "put", expiration=NEAR_EXP, iv=0.54),
    ]
    result = atm_iv_at_expiration(quotes, NEAR_EXP, Decimal("115"))
    assert result is not None
    assert result.atm_iv == Decimal("0.52")


def test_iv_term_structure_computes_positive_slope_for_upward_curve():
    quotes = [
        _quote(115, "call", expiration=NEAR_EXP, iv=0.40),
        _quote(115, "put", expiration=NEAR_EXP, iv=0.42),
        _quote(115, "call", expiration=NEXT_EXP, iv=0.50),
        _quote(115, "put", expiration=NEXT_EXP, iv=0.52),
    ]
    result = iv_term_structure(quotes, NEAR_EXP, NEXT_EXP, Decimal("115"))

    assert result.near_atm_iv == Decimal("0.41")
    assert result.next_atm_iv == Decimal("0.51")
    assert result.slope == Decimal("0.10")


def test_iv_term_structure_slope_none_when_next_expiration_missing_data():
    quotes = [
        _quote(115, "call", expiration=NEAR_EXP, iv=0.40),
        _quote(115, "put", expiration=NEAR_EXP, iv=0.42),
    ]
    result = iv_term_structure(quotes, NEAR_EXP, NEXT_EXP, Decimal("115"))

    assert result.near_atm_iv == Decimal("0.41")
    assert result.next_atm_iv is None
    assert result.slope is None
