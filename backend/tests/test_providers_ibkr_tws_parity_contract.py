"""IBKR TWS Migration Phase 1, Section 44 -- shared provider contract
test. The exact same golden quote (bid/ask/last/volume/open interest/IV/
Greeks/quality) is served through each real API's own wire format --
Client Portal Gateway REST JSON for the Web adapter, ibapi callback
values for TWS -- and both providers' resulting, normalized OptionQuote
objects are asserted field-for-field equal. This is what "parity" means
in this migration: not identical transport, but identical OUTPUT shape
for the application code that consumes it.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

import httpx

from providers.ibkr_client import IBKRClient
from providers.ibkr_options import IBKROptionsProvider
from providers.ibkr_tws_options import IBKRTWSProvider
from providers.types import SelectedLeg

AS_OF = datetime(2026, 9, 1, 20, 0, tzinfo=UTC)
TARGET_EXPIRATION = date(2026, 9, 18)

# The one golden scenario, expressed once, then re-encoded into each real
# provider's own wire format below -- never two independently-chosen
# "similar" values that could coincidentally match.
GOLDEN = {
    "bid": Decimal("4.90"),
    "ask": Decimal("5.10"),
    "last": Decimal("5.00"),
    "volume": 120,
    "open_interest": 340,
    "iv": Decimal("0.42"),
    "delta": Decimal("0.55"),
    "gamma": Decimal("0.02"),
    "theta": Decimal("-0.05"),
    "vega": Decimal("0.12"),
    "quality": "delayed",
}


# --- Web (Client Portal Gateway) side ---------------------------------

_WEB_UNDERLYING_CONID = 265598

_WEB_SEARCH_RESULT = [
    {
        "conid": str(_WEB_UNDERLYING_CONID),
        "symbol": "AAPL",
        "sections": [
            {"secType": "STK"},
            {"secType": "OPT", "months": "SEP26", "exchange": "SMART"},
        ],
    }
]

_WEB_SECDEF_INFO = [{"conid": 909090909, "strike": 150.0, "right": "C", "maturityDate": "20260918"}]

_WEB_SNAPSHOT = {
    "conid": 909090909,
    "84": "4.90",
    "86": "5.10",
    "31": "5.00",
    "87": "120",
    "87_raw": 120.0,
    "7638": "340",
    "7633": "42.0%",
    "7308": "0.55",
    "7309": "0.02",
    "7310": "-0.05",
    "7311": "0.12",
    "6509": "D",
}


def _web_route(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/v1/api/iserver/auth/status":
        return httpx.Response(
            200, json={"authenticated": True, "connected": True, "competing": False}
        )
    if path == "/v1/api/iserver/secdef/search":
        return httpx.Response(200, json=_WEB_SEARCH_RESULT)
    if path == "/v1/api/iserver/secdef/info":
        return httpx.Response(200, json=_WEB_SECDEF_INFO)
    if path == "/v1/api/iserver/marketdata/snapshot":
        return httpx.Response(200, json=[_WEB_SNAPSHOT])
    raise AssertionError(f"unexpected request: {request.url}")


def _web_quote(monkeypatch) -> object:
    monkeypatch.setattr("providers.ibkr_options.time.sleep", lambda _seconds: None)
    http_client = httpx.Client(
        base_url="https://localhost:5001/v1/api", transport=httpx.MockTransport(_web_route)
    )
    client = IBKRClient(base_url="https://localhost:5001/v1/api", client=http_client)
    provider = IBKROptionsProvider(base_url="https://localhost:5001/v1/api", client=client)
    legs = [SelectedLeg(strike=Decimal("150"), option_type="call", action="buy")]
    quotes = provider.get_quotes_for_selected_legs("AAPL", legs, TARGET_EXPIRATION, AS_OF)
    assert len(quotes) == 1
    return quotes[0]


# --- TWS side -----------------------------------------------------------


@dataclass
class _FakeContractRef:
    conId: int


@dataclass
class _FakeContractDetails:
    contract: _FakeContractRef


class _FakeTwsConnection:
    def ensure_connected(self) -> None:
        pass

    def request_contract_details(self, contract, timeout=None):
        return [_FakeContractDetails(contract=_FakeContractRef(conId=909090909))]

    def request_market_data_with_requirement(
        self, contract, requirement_satisfied, generic_ticks="", **kwargs
    ):
        return {
            "bid": 4.90,
            "ask": 5.10,
            "last": 5.00,
            "volume": 120,
            "open_interest": 340,
            "implied_volatility": 0.42,
            "delta": 0.55,
            "gamma": 0.02,
            "theta": -0.05,
            "vega": 0.12,
            "market_data_quality": "delayed",
        }


def _tws_quote() -> object:
    provider = IBKRTWSProvider(host="x", port=1, client_id=1, connection=_FakeTwsConnection())
    legs = [SelectedLeg(strike=Decimal("150"), option_type="call", action="buy")]
    quotes = provider.get_quotes_for_selected_legs("AAPL", legs, TARGET_EXPIRATION, AS_OF)
    assert len(quotes) == 1
    return quotes[0]


class TestWebAndTwsProduceIdenticalNormalizedOutput:
    def test_price_and_size_fields_match(self, monkeypatch):
        web = _web_quote(monkeypatch)
        tws = _tws_quote()
        assert web.bid == tws.bid == GOLDEN["bid"]
        assert web.ask == tws.ask == GOLDEN["ask"]
        assert web.last_price == tws.last_price == GOLDEN["last"]
        assert web.volume == tws.volume == GOLDEN["volume"]
        assert web.open_interest == tws.open_interest == GOLDEN["open_interest"]

    def test_greeks_and_iv_match(self, monkeypatch):
        web = _web_quote(monkeypatch)
        tws = _tws_quote()
        assert web.implied_volatility == tws.implied_volatility == GOLDEN["iv"]
        assert web.delta == tws.delta == GOLDEN["delta"]
        assert web.gamma == tws.gamma == GOLDEN["gamma"]
        assert web.theta == tws.theta == GOLDEN["theta"]
        assert web.vega == tws.vega == GOLDEN["vega"]

    def test_market_data_quality_matches(self, monkeypatch):
        web = _web_quote(monkeypatch)
        tws = _tws_quote()
        assert web.market_data_quality == tws.market_data_quality == GOLDEN["quality"]

    def test_identity_and_provenance_fields_are_present_on_both(self, monkeypatch):
        """IBKR TWS Migration, Phase 3 readiness (Section 8): source_
        provider is deliberately NOT identical between the two transports
        any more -- "ibkr_web" vs "ibkr_tws", so a persisted row honestly
        records which real transport served it (see providers/
        ibkr_options.py's own comment on the provenance rationale). This
        is a stricter check than the old identical-string assertion: it
        would catch a real mislabeling bug (e.g. TWS quotes tagged
        "ibkr_web") that an equality-between-web-and-tws check never
        could, since both used to be the bare "ibkr" unconditionally."""
        web = _web_quote(monkeypatch)
        tws = _tws_quote()
        assert web.source_provider == "ibkr_web"
        assert tws.source_provider == "ibkr_tws"
        for quote in (web, tws):
            assert quote.external_contract_id is not None
            assert isinstance(quote.external_contract_id, str)
            assert quote.strike == Decimal("150")
            assert quote.option_type == "call"
            assert quote.expiration_date == TARGET_EXPIRATION

    def test_both_quotes_are_the_same_real_ibkr_contract(self, monkeypatch):
        """Section 53 -- same real underlying contract identity, since
        this golden scenario deliberately used the identical conid
        (909090909) on both sides."""
        web = _web_quote(monkeypatch)
        tws = _tws_quote()
        assert web.external_contract_id == tws.external_contract_id == "909090909"
