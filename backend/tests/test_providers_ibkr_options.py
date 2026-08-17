import re
from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
import pytest

from providers.ibkr_client import IBKRClient, IBKRNotAuthenticatedError
from providers.ibkr_options import (
    IBKRContractNotFoundError,
    IBKROptionsProvider,
    _month_code,
    _next_month_code,
    _parse_abbreviated_int,
    _parse_percent,
    _parse_volume,
)

BASE_URL = "https://localhost:5001/v1/api"
AS_OF = datetime(2026, 8, 17, 20, 0, tzinfo=UTC)

# All real response shapes below are byte-for-byte what a real
# authenticated Gateway returned during Phase 13 live verification for
# NVDA (conid 4815747) -- see docs/ibkr_integration.md.

REAL_AUTH_STATUS = {"authenticated": True, "connected": True, "competing": False}

REAL_SECDEF_SEARCH_NVDA = [
    {
        "conid": "4815747",
        "companyHeader": "NVIDIA CORP - NASDAQ",
        "companyName": "NVIDIA CORP",
        "symbol": "NVDA",
        "description": "NASDAQ",
        "sections": [
            {"secType": "STK"},
            {
                "secType": "OPT",
                "months": "AUG26;SEP26;OCT26;NOV26;DEC26;JAN27",
                "exchange": "SMART",
            },
        ],
    },
    {
        "conid": "541229759",
        "companyName": "NVIDIA CORP-CDR",
        "symbol": "NVDA",
        "description": "TSE",
        "sections": [{"secType": "STK"}, {"secType": "OPT", "months": "AUG26", "exchange": "CDE"}],
    },
]

REAL_UNDERLYING_SNAPSHOT = [{"conid": 4815747, "31": "225.54", "6509": "DB"}]

REAL_STRIKES_AUG26 = {
    "call": [220.0, 222.5, 225.0, 227.5, 230.0],
    "put": [220.0, 222.5, 225.0, 227.5, 230.0],
}

# Real captured secdef/info shape for the 225 strike/call across every
# weekly expiration listed in AUG26 -- includes the same-day (already
# expiring) contract that must never be picked.
REAL_SECDEF_INFO_225_CALL = [
    {"conid": 907457759, "strike": 225.0, "right": "C", "maturityDate": "20260817"},
    {"conid": 907866760, "strike": 225.0, "right": "C", "maturityDate": "20260819"},
    {"conid": 863969805, "strike": 225.0, "right": "C", "maturityDate": "20260821"},
]

# Real captured option-snapshot rows for the 225 call/put at the 2026-08-19
# expiration (conids 907866760 / 907867812).
REAL_225_CALL_SNAPSHOT = {
    "conid": 907866760,
    "88": "280",
    "7310": "-0.622",
    "7311": "0.066",
    "7633": "31.4%",
    "6509": "ZBd",
    "7309": "0.077",
    "84": "2.41",
    "7308": "0.518",
    "7638": "8.01K",
    "31": "2.43",
    "86": "2.44",
    "87": "21.7K",
    "87_raw": 21700.0,
}
REAL_225_PUT_SNAPSHOT = {
    "conid": 907867812,
    "88": "5",
    "7310": "-0.598",
    "7311": "0.066",
    "7633": "31.4%",
    "6509": "ZBd",
    "7309": "0.077",
    "84": "1.88",
    "7308": "-0.483",
    "31": "1.89",
    "86": "1.89",
    "87": "41.0K",
    "87_raw": 41000.0,
    # no 7638 -- open interest genuinely absent on this contract in the
    # real captured response.
}


def _make_provider(base_url: str = BASE_URL, transport: httpx.MockTransport | None = None):
    http_client = httpx.Client(base_url=base_url, transport=transport) if transport else None
    client = IBKRClient(base_url=base_url, client=http_client)
    return IBKROptionsProvider(base_url=base_url, client=client)


class TestPureParsers:
    def test_month_code(self):
        assert _month_code(date(2026, 8, 17)) == "AUG26"
        assert _month_code(date(2027, 1, 1)) == "JAN27"

    def test_next_month_code_within_year(self):
        assert _next_month_code("AUG26") == "SEP26"

    def test_next_month_code_rolls_over_year(self):
        assert _next_month_code("DEC26") == "JAN27"

    def test_parse_percent_from_real_iv_string(self):
        assert _parse_percent("31.9%") == Decimal("0.319")

    def test_parse_percent_none(self):
        assert _parse_percent(None) is None
        assert _parse_percent("") is None

    def test_parse_abbreviated_int_thousands(self):
        assert _parse_abbreviated_int("4.12K") == 4120

    def test_parse_abbreviated_int_millions(self):
        assert _parse_abbreviated_int("65.5M") == 65_500_000

    def test_parse_abbreviated_int_plain(self):
        assert _parse_abbreviated_int("791") == 791

    def test_parse_abbreviated_int_none(self):
        assert _parse_abbreviated_int(None) is None

    def test_parse_volume_prefers_raw_field(self):
        assert _parse_volume({"87_raw": 6.55e7, "87": "65.5M"}) == 65_500_000

    def test_parse_volume_zero_is_not_dropped(self):
        # A real 0 must stay 0, not fall through to a different field --
        # regression test for a bug caught before this shipped (an `or`
        # chain would have treated 0 as falsy).
        assert _parse_volume({"87_raw": 0.0, "87": "65.5M"}) == 0

    def test_parse_volume_falls_back_to_display_string(self):
        assert _parse_volume({"87": "4.04K"}) == 4040


class TestGetOptionChainSimple:
    """Single-HTTP-call failure paths -- plain httpx_mock is fine here."""

    def test_raises_when_not_authenticated(self, httpx_mock):
        httpx_mock.add_response(
            url=re.compile(r".*/iserver/auth/status.*"),
            json={"authenticated": False, "connected": False, "competing": False},
        )
        provider = _make_provider()

        with pytest.raises(IBKRNotAuthenticatedError):
            provider.get_option_chain("NVDA", AS_OF)

    def test_raises_contract_not_found_for_unknown_ticker(self, httpx_mock):
        httpx_mock.add_response(url=re.compile(r".*/iserver/auth/status.*"), json=REAL_AUTH_STATUS)
        httpx_mock.add_response(url=re.compile(r".*/iserver/secdef/search.*"), json=[])
        provider = _make_provider()

        with pytest.raises(IBKRContractNotFoundError):
            provider.get_option_chain("ZZNOTAREALTICKER", AS_OF)


class TestGetOptionChainFullFlow:
    """Multi-step real discovery flow -- driven by a single deterministic
    routing function via httpx.MockTransport, since the real flow makes
    several requests to the same two endpoints (secdef/info,
    marketdata/snapshot) with different parameters each time; a plain
    queue of canned responses can't express "route by query params"
    without relying on undocumented mock-consumption ordering.
    """

    def _route(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        params = dict(httpx.QueryParams(request.url.query))

        if path == "/v1/api/iserver/auth/status":
            return httpx.Response(200, json=REAL_AUTH_STATUS)
        if path == "/v1/api/iserver/secdef/search":
            return httpx.Response(200, json=REAL_SECDEF_SEARCH_NVDA)
        if path == "/v1/api/iserver/secdef/strikes":
            return httpx.Response(200, json=REAL_STRIKES_AUG26)
        if path == "/v1/api/iserver/secdef/info":
            strike, right = params["strike"], params["right"]
            if strike == "225.0" and right == "C":
                return httpx.Response(200, json=REAL_SECDEF_INFO_225_CALL)
            if strike == "225.0" and right == "P":
                return httpx.Response(
                    200,
                    json=[
                        {
                            "conid": 907867812,
                            "strike": 225.0,
                            "right": "P",
                            "maturityDate": "20260819",
                        }
                    ],
                )
            # every other strike/right resolves to exactly one contract at
            # the same 2026-08-19 target expiration.
            conid = 900_000_000 + abs(hash((strike, right))) % 90_000_000
            return httpx.Response(
                200,
                json=[
                    {
                        "conid": conid,
                        "strike": float(strike),
                        "right": right,
                        "maturityDate": "20260819",
                    }
                ],
            )
        if path == "/v1/api/iserver/marketdata/snapshot":
            conids = params["conids"].split(",")
            if conids == ["4815747"]:
                return httpx.Response(200, json=REAL_UNDERLYING_SNAPSHOT)
            rows = []
            for conid_str in conids:
                if int(conid_str) == 907866760:
                    rows.append(REAL_225_CALL_SNAPSHOT)
                elif int(conid_str) == 907867812:
                    rows.append(REAL_225_PUT_SNAPSHOT)
                else:
                    # unsubscribed/priming-call shape: identity fields only
                    rows.append({"conid": int(conid_str), "conidEx": conid_str})
            return httpx.Response(200, json=rows)
        raise AssertionError(f"unexpected request in test: {request.url}")

    def _provider(self, strikes_window: int = 2) -> IBKROptionsProvider:
        import providers.ibkr_options as ibkr_options_module

        ibkr_options_module.STRIKES_AROUND_ATM = strikes_window
        return _make_provider(transport=httpx.MockTransport(self._route))

    def test_returns_empty_when_no_strikes_found(self, monkeypatch):
        def empty_strikes_route(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/api/iserver/secdef/strikes":
                return httpx.Response(200, json={"call": [], "put": []})
            return self._route(request)

        provider = _make_provider(transport=httpx.MockTransport(empty_strikes_route))

        assert provider.get_option_chain("NVDA", AS_OF, reference_date=date(2026, 8, 20)) == []

    def test_full_real_flow_returns_parsed_quotes(self):
        provider = self._provider(strikes_window=2)

        quotes = provider.get_option_chain("NVDA", AS_OF, reference_date=date(2026, 8, 17))

        assert len(quotes) >= 2
        call = next(q for q in quotes if q.option_type == "call" and q.strike == Decimal("225.0"))
        assert call.bid == Decimal("2.41")
        assert call.ask == Decimal("2.44")
        assert call.delta == Decimal("0.518")
        assert call.implied_volatility == Decimal("0.314")
        assert call.open_interest == 8010
        assert call.volume == 21700
        assert call.market_data_quality == "frozen"
        assert call.external_contract_id == "907866760"
        assert call.source_provider == "ibkr"
        assert call.expiration_date == date(2026, 8, 19)

        put = next(q for q in quotes if q.option_type == "put" and q.strike == Decimal("225.0"))
        assert put.open_interest is None  # genuinely absent, not fabricated as 0
        assert put.delta == Decimal("-0.483")

    def test_never_selects_same_day_expiration(self):
        provider = self._provider(strikes_window=2)

        quotes = provider.get_option_chain("NVDA", AS_OF, reference_date=date(2026, 8, 17))

        assert all(q.expiration_date != date(2026, 8, 17) for q in quotes)
