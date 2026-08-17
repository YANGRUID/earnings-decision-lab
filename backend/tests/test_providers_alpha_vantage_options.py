import re
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from providers.alpha_vantage import AlphaVantageError
from providers.alpha_vantage_options import (
    AlphaVantageOptionsProvider,
    PremiumEndpointRequiredError,
)

# The exact response captured live against the real API during Phase 12
# development (see docs/engineering_decisions.md) -- Alpha Vantage's own
# artificial sample data for a premium endpoint this project's plan
# doesn't have. Fake contract IDs, an invalid "2099-99-99" expiration --
# this must never be parsed as if it were real.
REAL_PREMIUM_GATE_RESPONSE = {
    "endpoint": "Realtime Options",
    "message": (
        "This is a premium endpoint. ***THE SAMPLE DATA SCHEMA BELOW IS ARTIFICIAL AND "
        "FOR ILLUSTRATION PURPOSES ONLY***. To access the actual data, please subscribe "
        "to either the 600 requests per minute or the 1200 requests per minute premium "
        "plan at https://www.alphavantage.co/premium/ if you would like to access "
        "realtime US options data for personal non-professional use."
    ),
    "data": [
        {
            "contractID": "XXYYZZ999999C00020000",
            "symbol": "XXYYZZ",
            "expiration": "2099-99-99",
            "strike": "20.00",
            "type": "call",
            "last": "100.00",
            "bid": "100.05",
            "ask": "100.15",
            "volume": "100",
            "open_interest": "100",
        }
    ],
}

# A plausible real-shape response (following AV's documented field names
# with require_greeks=true) -- unverified against an actual subscription,
# used to prove the parsing logic itself works correctly once/if this
# project ever has real data to parse. See module docstring.
PLAUSIBLE_REAL_RESPONSE = {
    "endpoint": "Realtime Options",
    "message": "success",
    "data": [
        {
            "contractID": "MU260918C00120000",
            "symbol": "MU",
            "expiration": "2026-09-18",
            "strike": "120.00",
            "type": "call",
            "last": "8.40",
            "bid": "8.30",
            "ask": "8.50",
            "volume": "532",
            "open_interest": "1204",
            "implied_volatility": "0.55",
            "delta": "0.62",
            "gamma": "0.018",
            "theta": "-0.12",
            "vega": "0.31",
        },
        {
            "contractID": "MU260918P00120000",
            "symbol": "MU",
            "expiration": "2026-09-18",
            "strike": "120.00",
            "type": "put",
            "last": "7.10",
            "bid": "7.00",
            "ask": "7.20",
            "volume": "410",
            "open_interest": "980",
            "implied_volatility": "0.53",
            "delta": "-0.38",
            "gamma": "0.017",
            "theta": "-0.11",
            "vega": "0.30",
        },
    ],
}

AS_OF = datetime(2026, 8, 17, 15, 0, tzinfo=UTC)


def test_requires_api_key():
    with pytest.raises(ValueError):
        AlphaVantageOptionsProvider(api_key="")


def test_real_premium_gate_response_raises_and_never_returns_fake_data(httpx_mock):
    httpx_mock.add_response(
        url=re.compile(r"https://www\.alphavantage\.co/query.*"), json=REAL_PREMIUM_GATE_RESPONSE
    )
    provider = AlphaVantageOptionsProvider(api_key="test-key")

    with pytest.raises(PremiumEndpointRequiredError):
        provider.get_option_chain("MU", AS_OF)


def test_premium_gate_error_is_an_alpha_vantage_error(httpx_mock):
    # PremiumEndpointRequiredError must be catchable by any code that
    # already handles AlphaVantageError generically (e.g. the fallback
    # chain in providers/fallback.py).
    httpx_mock.add_response(
        url=re.compile(r"https://www\.alphavantage\.co/query.*"), json=REAL_PREMIUM_GATE_RESPONSE
    )
    provider = AlphaVantageOptionsProvider(api_key="test-key")

    with pytest.raises(AlphaVantageError):
        provider.get_option_chain("MU", AS_OF)


def test_parses_plausible_real_response_including_greeks(httpx_mock):
    httpx_mock.add_response(
        url=re.compile(r"https://www\.alphavantage\.co/query.*"), json=PLAUSIBLE_REAL_RESPONSE
    )
    provider = AlphaVantageOptionsProvider(api_key="test-key")

    quotes = provider.get_option_chain("MU", AS_OF)

    assert len(quotes) == 2
    call = next(q for q in quotes if q.option_type == "call")
    assert call.strike == Decimal("120.00")
    assert call.bid == Decimal("8.30")
    assert call.ask == Decimal("8.50")
    assert call.implied_volatility == Decimal("0.55")
    assert call.delta == Decimal("0.62")
    assert call.gamma == Decimal("0.018")
    assert call.theta == Decimal("-0.12")
    assert call.vega == Decimal("0.31")
    assert call.source_provider == "alpha_vantage"

    put = next(q for q in quotes if q.option_type == "put")
    assert put.delta == Decimal("-0.38")


def test_unexpected_response_shape_raises_alpha_vantage_error(httpx_mock):
    httpx_mock.add_response(
        url=re.compile(r"https://www\.alphavantage\.co/query.*"),
        json={"Note": "Thank you for using Alpha Vantage! Rate limit reached."},
    )
    provider = AlphaVantageOptionsProvider(api_key="test-key")

    with pytest.raises(AlphaVantageError):
        provider.get_option_chain("MU", AS_OF)
