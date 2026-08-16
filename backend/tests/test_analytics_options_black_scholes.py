import math

import pytest

from analytics.options.black_scholes import price_and_greeks
from models.enums import OptionType


def test_matches_known_textbook_values():
    # S=100, K=100, T=1y, r=5%, vol=20%, q=0 — classic Hull reference case.
    call = price_and_greeks(OptionType.CALL, 100, 100, 1.0, 0.05, 0.20)
    put = price_and_greeks(OptionType.PUT, 100, 100, 1.0, 0.05, 0.20)

    assert call.price == pytest.approx(10.4506, abs=0.01)
    assert put.price == pytest.approx(5.5735, abs=0.01)
    assert call.delta == pytest.approx(0.6368, abs=0.01)
    assert put.delta == pytest.approx(-0.3632, abs=0.01)


def test_put_call_parity_holds():
    # C - P = S*e^(-qT) - K*e^(-rT), for any valid parameter set.
    for spot, strike, t, r, vol, q in [
        (100, 100, 1.0, 0.05, 0.20, 0.0),
        (971.66, 900, 0.25, 0.045, 0.55, 0.0),
        (50, 60, 0.5, 0.03, 0.35, 0.01),
    ]:
        call = price_and_greeks(OptionType.CALL, spot, strike, t, r, vol, q)
        put = price_and_greeks(OptionType.PUT, spot, strike, t, r, vol, q)
        lhs = call.price - put.price
        rhs = spot * math.exp(-q * t) - strike * math.exp(-r * t)
        assert lhs == pytest.approx(rhs, abs=1e-6)


def test_gamma_and_vega_identical_for_call_and_put():
    # Known BS identity: gamma and vega don't depend on option type.
    call = price_and_greeks(OptionType.CALL, 100, 105, 0.5, 0.04, 0.3)
    put = price_and_greeks(OptionType.PUT, 100, 105, 0.5, 0.04, 0.3)

    assert call.gamma == pytest.approx(put.gamma, rel=1e-9)
    assert call.vega == pytest.approx(put.vega, rel=1e-9)


def test_deep_itm_call_delta_approaches_one():
    result = price_and_greeks(OptionType.CALL, 200, 50, 0.1, 0.05, 0.2)
    assert result.delta > 0.99


def test_deep_otm_call_delta_approaches_zero():
    result = price_and_greeks(OptionType.CALL, 50, 200, 0.1, 0.05, 0.2)
    assert result.delta < 0.01


@pytest.mark.parametrize(
    "kwargs",
    [
        {"time_to_expiry_years": 0},
        {"time_to_expiry_years": -0.1},
        {"vol": 0},
        {"vol": -0.1},
        {"spot": 0},
        {"strike": -10},
    ],
)
def test_rejects_invalid_inputs(kwargs):
    params = {
        "option_type": OptionType.CALL,
        "spot": 100,
        "strike": 100,
        "time_to_expiry_years": 1.0,
        "rate": 0.05,
        "vol": 0.2,
    }
    params.update(kwargs)
    with pytest.raises(ValueError):
        price_and_greeks(**params)
