from decimal import Decimal

import httpx
import pytest

from providers.ibkr_client import IBKRClient, IBKRNotAuthenticatedError
from providers.ibkr_portfolio import IBKRPortfolioProvider

BASE_URL = "https://localhost:5001/v1/api"
REAL_AUTH_STATUS = {"authenticated": True, "connected": True, "competing": False}

# Real response shape for /iserver/accounts, captured live during Phase 13
# verification (account number replaced with an obviously-fake value here
# -- the real one is never committed anywhere in this repository).
REAL_ACCOUNTS_RESPONSE = {
    "accounts": ["U99999999"],
    "acctProps": {"U99999999": {"hasChildAccounts": False}},
    "selectedAccount": "U99999999",
}

# Real position shape per IBKR's own documented example (a futures
# position) -- see docs/ibkr_integration.md.
REAL_POSITION_ROW = {
    "acctId": "U99999999",
    "conid": 672387468,
    "contractDesc": "MNQ MAR2025",
    "position": 2.0,
    "mktPrice": 21770.4296875,
    "mktValue": 87081.72,
    "currency": "USD",
    "avgCost": 43536.12,
    "avgPrice": 21768.06,
    "realizedPnl": 0.0,
    "unrealizedPnl": 9.48,
    "expiry": None,
    "putOrCall": None,
    "multiplier": None,
    "strike": 0.0,
    "assetClass": "FUT",
}

REAL_OPTION_POSITION_ROW = {
    "acctId": "U99999999",
    "conid": 907866760,
    "contractDesc": "NVDA AUG2619 225 C",
    "position": -1.0,
    "mktPrice": 2.43,
    "mktValue": -243.0,
    "currency": "USD",
    "avgCost": 200.0,
    "realizedPnl": 0.0,
    "unrealizedPnl": -43.0,
    "expiry": "20260819",
    "putOrCall": "C",
    "strike": 225.0,
    "assetClass": "OPT",
}


def _route(*, accounts=REAL_ACCOUNTS_RESPONSE, positions_by_page=None):
    positions_by_page = positions_by_page if positions_by_page is not None else {0: []}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/api/iserver/auth/status":
            return httpx.Response(200, json=REAL_AUTH_STATUS)
        if path == "/v1/api/iserver/accounts":
            return httpx.Response(200, json=accounts)
        if path.startswith("/v1/api/portfolio/") and "/positions/" in path:
            page = int(path.rsplit("/", 1)[-1])
            return httpx.Response(200, json=positions_by_page.get(page, []))
        raise AssertionError(f"unexpected request: {request.url}")

    return handler


def _provider(handler) -> IBKRPortfolioProvider:
    http_client = httpx.Client(base_url=BASE_URL, transport=httpx.MockTransport(handler))
    client = IBKRClient(base_url=BASE_URL, client=http_client)
    return IBKRPortfolioProvider(base_url=BASE_URL, client=client)


class TestGetMaskedAccountId:
    def test_raises_when_not_authenticated(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/api/iserver/auth/status":
                return httpx.Response(
                    200, json={"authenticated": False, "connected": False, "competing": False}
                )
            raise AssertionError("should not reach accounts endpoint")

        provider = _provider(handler)
        with pytest.raises(IBKRNotAuthenticatedError):
            provider.get_masked_account_id()

    def test_masks_the_real_account_id(self):
        provider = _provider(_route())
        assert provider.get_masked_account_id() == "U99****99"

    def test_empty_string_when_no_accounts(self):
        provider = _provider(_route(accounts={"accounts": []}))
        assert provider.get_masked_account_id() == ""


class TestGetPositions:
    def test_empty_account_returns_empty_list(self):
        provider = _provider(_route(positions_by_page={0: []}))
        assert provider.get_positions() == []

    def test_parses_real_futures_position(self):
        provider = _provider(_route(positions_by_page={0: [REAL_POSITION_ROW]}))

        positions = provider.get_positions()

        assert len(positions) == 1
        p = positions[0]
        assert p.account_id_masked == "U99****99"
        assert p.conid == 672387468
        assert p.contract_description == "MNQ MAR2025"
        assert p.asset_class == "FUT"
        assert p.quantity == 2
        assert p.market_value == Decimal("87081.72")
        assert p.unrealized_pnl == Decimal("9.48")
        assert p.option_expiry is None
        assert p.source_provider == "ibkr"

    def test_parses_real_option_position_with_identification_fields(self):
        provider = _provider(_route(positions_by_page={0: [REAL_OPTION_POSITION_ROW]}))

        positions = provider.get_positions()

        assert len(positions) == 1
        p = positions[0]
        assert p.asset_class == "OPT"
        assert p.quantity == -1  # short position -- a real negative quantity
        assert p.option_expiry == "20260819"
        assert p.option_right == "C"
        assert p.option_strike == 225

    def test_paginates_when_a_full_page_is_returned(self):
        full_page = [{**REAL_POSITION_ROW, "conid": i} for i in range(100)]
        provider = _provider(
            _route(positions_by_page={0: full_page, 1: [REAL_OPTION_POSITION_ROW]})
        )

        positions = provider.get_positions()

        assert len(positions) == 101

    def test_never_leaks_the_real_unmasked_account_id(self):
        provider = _provider(_route(positions_by_page={0: [REAL_POSITION_ROW]}))

        positions = provider.get_positions()

        for p in positions:
            assert "U99999999" not in p.account_id_masked
            assert p.account_id_masked == "U99****99"
