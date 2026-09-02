import re
from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
import pytest

from models.enums import QuoteRequirement
from providers.ibkr_client import IBKRClient, IBKRNotAuthenticatedError
from providers.ibkr_options import (
    IBKRContractNotFoundError,
    IBKROptionsProvider,
    _decimal_or_none,
    _month_code,
    _next_month_code,
    _parse_abbreviated_int,
    _parse_percent,
    _parse_volume,
    entry_requirement_for_action,
    exit_requirement_for_action,
)
from providers.types import KnownContract, SelectedLeg

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

    def test_decimal_or_none_strips_a_real_ibkr_status_prefix(self):
        """Regression test: confirmed live (2026-08-25, market closed)
        that /iserver/marketdata/snapshot's field 31 ("Last") returned
        "C208.48" for a real, valid NVDA close price, not "208.48" --
        the leading "C" made this silently unparseable before this fix,
        discarding real quote data across every price/Greek field this
        adapter reads (they all share the same snapshot endpoint)."""
        assert _decimal_or_none("C208.48") == Decimal("208.48")
        assert _decimal_or_none("H100.5") == Decimal("100.5")

    def test_decimal_or_none_plain_numeric_unaffected(self):
        assert _decimal_or_none("208.48") == Decimal("208.48")
        assert _decimal_or_none("-1.5") == Decimal("-1.5")

    def test_decimal_or_none_none_and_empty(self):
        assert _decimal_or_none(None) is None
        assert _decimal_or_none("") is None

    def test_decimal_or_none_prefix_only_is_none(self):
        assert _decimal_or_none("C") is None
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
        assert call.source_provider == "ibkr_web"
        assert call.expiration_date == date(2026, 8, 19)

        put = next(q for q in quotes if q.option_type == "put" and q.strike == Decimal("225.0"))
        assert put.open_interest is None  # genuinely absent, not fabricated as 0
        assert put.delta == Decimal("-0.483")

    def test_never_selects_same_day_expiration(self):
        provider = self._provider(strikes_window=2)

        quotes = provider.get_option_chain("NVDA", AS_OF, reference_date=date(2026, 8, 17))

        assert all(q.expiration_date != date(2026, 8, 17) for q in quotes)

    def test_general_mode_allows_same_day_expiration(self):
        """Regression test for the general/current (non-earnings-anchored)
        collection path added to unblock a ticker with no known upcoming
        earnings date (real bug: AMD, 2026-08-18) -- earnings_anchored=False
        must use the on-or-after rule, not silently keep the
        strictly-after earnings rule.
        """
        provider = self._provider(strikes_window=2)

        quotes = provider.get_option_chain(
            "NVDA", AS_OF, reference_date=date(2026, 8, 17), earnings_anchored=False
        )

        assert len(quotes) >= 1
        assert all(q.expiration_date == date(2026, 8, 17) for q in quotes)

    def test_auto_mode_calls_secdef_strikes_exactly_once(self):
        """Live market-data validation (2026-08-26) -- AUTO mode
        (``expiration=None``) used to call ``/iserver/secdef/strikes``
        twice for the exact same (conid, month): once inside
        ``_resolve_target_expiration`` (needed for a probe strike) and a
        second, redundant time right after in ``get_option_chain`` itself.
        ``_resolve_target_expiration`` now returns the strikes it already
        fetched so the second call never happens."""
        counts = {"strikes": 0}

        def counting_route(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/api/iserver/secdef/strikes":
                counts["strikes"] += 1
            return self._route(request)

        provider = _make_provider(transport=httpx.MockTransport(counting_route))

        quotes = provider.get_option_chain("NVDA", AS_OF, reference_date=date(2026, 8, 17))

        assert len(quotes) >= 1
        assert counts["strikes"] == 1

    def test_manual_expiration_still_calls_secdef_strikes_once(self):
        """The manual-expiration branch (``expiration=`` given) never went
        through ``_resolve_target_expiration`` at all, so it only ever
        made one real ``/iserver/secdef/strikes`` call -- confirmed
        unchanged by the AUTO-mode fix above."""
        counts = {"strikes": 0}

        def counting_route(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/api/iserver/secdef/strikes":
                counts["strikes"] += 1
            return self._route(request)

        provider = _make_provider(transport=httpx.MockTransport(counting_route))

        quotes = provider.get_option_chain("NVDA", AS_OF, expiration=date(2026, 8, 19))

        assert len(quotes) >= 1
        assert counts["strikes"] == 1


class TestSnapshotWarmup:
    """Post-live correction (2026-08-25) -- _snapshot_with_warmup's real,
    bounded, validating retry, replacing the old fixed single 2.0s wait.
    See that method's own docstring, and _SNAPSHOT_WARMUP_MAX_ATTEMPTS's,
    for the real Aug 25 evidence (EntrySnapshot rows with a resolved
    conid + a real market-data-quality code but empty bid/ask/last)
    this exists to fix."""

    def test_retries_until_the_last_price_field_actually_arrives(self):
        """Real, observed IBKR shape: the first N snapshot responses for
        a freshly-subscribed conid carry only identity fields; a later
        one carries the real last price. This must not be accepted (or
        given up on) before that real value has a chance to arrive."""
        call_count = {"snapshot": 0}

        def route(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path == "/v1/api/iserver/auth/status":
                return httpx.Response(200, json=REAL_AUTH_STATUS)
            if path == "/v1/api/iserver/marketdata/snapshot":
                call_count["snapshot"] += 1
                # Priming call (#1) and the next 2 real polls (#2, #3)
                # come back identity-only; the 4th call is the first one
                # with real data -- still well inside the real bound.
                if call_count["snapshot"] < 4:
                    return httpx.Response(200, json=[{"conid": 4815747, "conidEx": "4815747"}])
                return httpx.Response(200, json=REAL_UNDERLYING_SNAPSHOT)
            raise AssertionError(f"unexpected request in test: {request.url}")

        provider = _make_provider(transport=httpx.MockTransport(route))

        price, quality = provider._underlying_quote(4815747)  # noqa: SLF001 -- unit-testing the real retry algorithm directly

        assert price == Decimal("225.54")
        assert quality == "delayed"
        # 1 priming call + at least 3 real polls to reach the 4th
        # response -- a real, bounded number of calls, not unbounded.
        assert call_count["snapshot"] == 4

    def test_gives_up_honestly_after_the_bounded_attempt_count(self):
        """A conid whose real data never arrives within the bound must
        still return the honest "no data" answer this project already
        reports -- never hang, never fabricate a value, and never make
        an unbounded number of real IBKR calls."""
        call_count = {"snapshot": 0}

        def route(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path == "/v1/api/iserver/auth/status":
                return httpx.Response(200, json=REAL_AUTH_STATUS)
            if path == "/v1/api/iserver/marketdata/snapshot":
                call_count["snapshot"] += 1
                return httpx.Response(200, json=[{"conid": 4815747, "conidEx": "4815747"}])
            raise AssertionError(f"unexpected request in test: {request.url}")

        provider = _make_provider(transport=httpx.MockTransport(route))

        price, quality = provider._underlying_quote(4815747)  # noqa: SLF001 -- unit-testing the real retry algorithm directly

        assert price is None
        assert quality == "unknown"
        # 1 priming call + exactly _SNAPSHOT_WARMUP_MAX_ATTEMPTS real
        # polls -- bounded, real, never infinite.
        from providers.ibkr_options import _SNAPSHOT_WARMUP_MAX_ATTEMPTS

        assert call_count["snapshot"] == 1 + _SNAPSHOT_WARMUP_MAX_ATTEMPTS

    def test_a_fully_populated_first_real_response_needs_no_extra_polling(self):
        """The common, real case (a genuinely liquid, already-warm
        conid): the first poll after priming already has real data --
        this must not retry needlessly."""
        call_count = {"snapshot": 0}

        def route(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path == "/v1/api/iserver/auth/status":
                return httpx.Response(200, json=REAL_AUTH_STATUS)
            if path == "/v1/api/iserver/marketdata/snapshot":
                call_count["snapshot"] += 1
                return httpx.Response(200, json=REAL_UNDERLYING_SNAPSHOT)
            raise AssertionError(f"unexpected request in test: {request.url}")

        provider = _make_provider(transport=httpx.MockTransport(route))

        price, _quality = provider._underlying_quote(4815747)  # noqa: SLF001 -- unit-testing the real retry algorithm directly

        assert price == Decimal("225.54")
        assert call_count["snapshot"] == 2  # priming call + exactly one real poll


class TestRequirementMapping:
    """IBKR execution-observability hardening (2026-08-26) -- the real
    action -> executable-side rule, mirrored from benchmark_entry_
    capture.py::_price_leg (entry) and benchmark_exit_capture.py::
    _price_exit_leg (exit, the exact inverse)."""

    def test_entry_buy_requires_ask(self):
        assert entry_requirement_for_action("buy") == QuoteRequirement.ASK

    def test_entry_sell_requires_bid(self):
        assert entry_requirement_for_action("sell") == QuoteRequirement.BID

    def test_entry_unknown_action_is_analytical_never_guessed(self):
        assert entry_requirement_for_action(None) == QuoteRequirement.ANALYTICAL
        assert entry_requirement_for_action("short") == QuoteRequirement.ANALYTICAL

    def test_exit_of_a_buy_leg_requires_bid(self):
        """Closing a long (BUY) leg means selling it -- BID."""
        assert exit_requirement_for_action("buy") == QuoteRequirement.BID

    def test_exit_of_a_sell_leg_requires_ask(self):
        """Closing a short (SELL) leg means buying it back -- ASK."""
        assert exit_requirement_for_action("sell") == QuoteRequirement.ASK

    def test_exit_unknown_action_is_analytical_never_guessed(self):
        assert exit_requirement_for_action(None) == QuoteRequirement.ANALYTICAL


class TestExecutableSideAwareWarmup:
    """IBKR execution-observability hardening (2026-08-26) -- the real
    readiness bug this task's own audit confirmed from source:
    _snapshot_with_warmup's exit condition only ever checked LAST, so a
    LONG entry's real ASK could still be missing on a poll this method
    would already accept. These tests drive _snapshot_with_warmup
    directly (the same "unit-test the real retry algorithm" precedent
    TestSnapshotWarmup already uses) with a real ``required`` map, using
    scripted multi-attempt responses -- LAST-only, then LAST+one side,
    then both sides -- to prove the exit condition genuinely waits for
    the specific field each conid's own executable side needs."""

    def _scripted_provider(self, responses: list[list[dict]]) -> IBKROptionsProvider:
        call_count = {"snapshot": 0}

        def route(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path == "/v1/api/iserver/auth/status":
                return httpx.Response(200, json=REAL_AUTH_STATUS)
            if path == "/v1/api/iserver/marketdata/snapshot":
                idx = min(call_count["snapshot"], len(responses) - 1)
                call_count["snapshot"] += 1
                return httpx.Response(200, json=responses[idx])
            raise AssertionError(f"unexpected request in test: {request.url}")

        provider = _make_provider(transport=httpx.MockTransport(route))
        provider._test_call_count = call_count  # type: ignore[attr-defined]
        return provider

    def test_long_entry_continues_past_last_until_ask_arrives(self):
        """Attempt 1: LAST present, ASK missing -- must continue. Attempt
        2: ASK present -- must stop successfully. Exactly the example in
        this task's own Section 2."""
        responses = [
            [{"conid": 907866760, "conidEx": "907866760"}],  # priming
            [{"conid": 907866760, "31": "6.35", "84": "6.20"}],  # LAST+BID, no ASK
            [{"conid": 907866760, "31": "6.35", "84": "6.20", "86": "6.50"}],  # ASK arrives
        ]
        provider = self._scripted_provider(responses)

        data = provider._snapshot_with_warmup(  # noqa: SLF001 -- unit-testing the real retry algorithm directly
            [907866760], fields="31,84,86", required={907866760: QuoteRequirement.ASK}
        )

        assert _decimal_or_none(data[0]["86"]) == Decimal("6.50")
        assert provider._test_call_count["snapshot"] == 3  # type: ignore[attr-defined]

    def test_short_entry_continues_past_last_until_bid_arrives(self):
        responses = [
            [{"conid": 907867812, "conidEx": "907867812"}],  # priming
            [{"conid": 907867812, "31": "1.89", "86": "1.90"}],  # LAST+ASK, no BID
            [{"conid": 907867812, "31": "1.89", "86": "1.90", "84": "1.88"}],  # BID arrives
        ]
        provider = self._scripted_provider(responses)

        data = provider._snapshot_with_warmup(  # noqa: SLF001 -- unit-testing the real retry algorithm directly
            [907867812], fields="31,84,86", required={907867812: QuoteRequirement.BID}
        )

        assert _decimal_or_none(data[0]["84"]) == Decimal("1.88")
        assert provider._test_call_count["snapshot"] == 3  # type: ignore[attr-defined]

    def test_ask_never_arrives_exhausts_attempts_and_reports_honestly(self):
        """No fallback, no fabricated price -- the bounded attempt count
        is respected exactly as before, and the caller sees the real,
        still-incomplete last response, never a guessed ASK."""
        row = {"conid": 907866760, "31": "6.35", "84": "6.20"}  # ASK never shows up
        provider = self._scripted_provider([[row], [row]])

        data = provider._snapshot_with_warmup(  # noqa: SLF001 -- unit-testing the real retry algorithm directly
            [907866760], fields="31,84,86", required={907866760: QuoteRequirement.ASK}
        )

        assert _decimal_or_none(data[0].get("86")) is None
        from providers.ibkr_options import _SNAPSHOT_WARMUP_MAX_ATTEMPTS

        assert provider._test_call_count["snapshot"] == 1 + _SNAPSHOT_WARMUP_MAX_ATTEMPTS  # type: ignore[attr-defined]

    def test_bid_never_arrives_exhausts_attempts_and_reports_honestly(self):
        row = {"conid": 907867812, "31": "1.89", "86": "1.90"}  # BID never shows up
        provider = self._scripted_provider([[row], [row]])

        data = provider._snapshot_with_warmup(  # noqa: SLF001 -- unit-testing the real retry algorithm directly
            [907867812], fields="31,84,86", required={907867812: QuoteRequirement.BID}
        )

        assert _decimal_or_none(data[0].get("84")) is None
        from providers.ibkr_options import _SNAPSHOT_WARMUP_MAX_ATTEMPTS

        assert provider._test_call_count["snapshot"] == 1 + _SNAPSHOT_WARMUP_MAX_ATTEMPTS  # type: ignore[attr-defined]

    def test_multi_leg_each_conid_independently_requires_its_own_side(self):
        """A real long call butterfly's shape: long lower call needs ASK,
        short middle call needs BID. "Some field exists" or "one leg is
        complete" must never be treated as sufficient -- both legs' own
        real requirement must be independently satisfied."""
        long_conid, short_conid = 111, 222
        responses = [
            [  # priming
                {"conid": long_conid, "conidEx": "111"},
                {"conid": short_conid, "conidEx": "222"},
            ],
            [  # long leg's ASK still missing; short leg's BID already there
                {"conid": long_conid, "31": "6.35", "84": "6.20"},
                {"conid": short_conid, "31": "3.05", "84": "2.90", "86": "3.10"},
            ],
            [  # long leg's ASK finally arrives
                {"conid": long_conid, "31": "6.35", "84": "6.20", "86": "6.50"},
                {"conid": short_conid, "31": "3.05", "84": "2.90", "86": "3.10"},
            ],
        ]
        provider = self._scripted_provider(responses)

        data = provider._snapshot_with_warmup(  # noqa: SLF001 -- unit-testing the real retry algorithm directly
            [long_conid, short_conid],
            fields="31,84,86",
            required={long_conid: QuoteRequirement.ASK, short_conid: QuoteRequirement.BID},
        )

        by_conid = {row["conid"]: row for row in data}
        assert _decimal_or_none(by_conid[long_conid]["86"]) == Decimal("6.50")
        assert _decimal_or_none(by_conid[short_conid]["84"]) == Decimal("2.90")
        # Stopped as soon as BOTH were independently satisfied (attempt
        # 3), not earlier just because the short leg was already done
        # on attempt 2.
        assert provider._test_call_count["snapshot"] == 3  # type: ignore[attr-defined]

    def test_analytical_default_unaffected_by_missing_bid_ask(self):
        """No ``required`` passed at all (every caller before this
        hardening pass, and get_option_chain's own analytical path
        today) -- LAST alone still ends the poll, byte-identical to the
        pre-hardening behavior, even with bid/ask genuinely absent."""
        row = {"conid": 4815747, "31": "225.54"}  # last only, no bid/ask
        provider = self._scripted_provider([[row], [row]])

        data = provider._snapshot_with_warmup([4815747], fields="31,84,86")  # noqa: SLF001

        assert _decimal_or_none(data[0]["31"]) == Decimal("225.54")
        assert provider._test_call_count["snapshot"] == 2  # type: ignore[attr-defined]


class TestExecutableSideAwareEntryAndExitIntegration:
    """End-to-end proof (not just the low-level warmup unit) that
    SelectedLeg.action / KnownContract.action actually flow through
    get_quotes_for_selected_legs / get_quotes_for_known_contracts into a
    real required-side wait -- the exact wiring
    services/benchmark_entry_capture.py and services/benchmark_exit_
    capture.py now depend on."""

    def _route(self, snapshot_responses: list[list[dict]]):
        call_count = {"snapshot": 0}

        def route(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            params = dict(httpx.QueryParams(request.url.query))
            if path == "/v1/api/iserver/auth/status":
                return httpx.Response(200, json=REAL_AUTH_STATUS)
            if path == "/v1/api/iserver/secdef/search":
                return httpx.Response(200, json=REAL_SECDEF_SEARCH_NVDA)
            if path == "/v1/api/iserver/secdef/info":
                strike, right = params["strike"], params["right"]
                assert strike == "225.0" and right == "C"
                return httpx.Response(200, json=REAL_SECDEF_INFO_225_CALL)
            if path == "/v1/api/iserver/marketdata/snapshot":
                idx = min(call_count["snapshot"], len(snapshot_responses) - 1)
                call_count["snapshot"] += 1
                return httpx.Response(200, json=snapshot_responses[idx])
            raise AssertionError(f"unexpected request in test: {request.url}")

        route.call_count = call_count  # type: ignore[attr-defined]
        return route

    def test_get_quotes_for_selected_legs_waits_for_ask_on_a_buy_leg(self):
        conid = 907866760
        route = self._route(
            [
                [{"conid": conid, "31": "6.35", "84": "6.20"}],  # LAST+BID, no ASK yet
                [{"conid": conid, "31": "6.35", "84": "6.20", "86": "6.50"}],  # ASK arrives
            ]
        )
        provider = _make_provider(transport=httpx.MockTransport(route))

        quotes = provider.get_quotes_for_selected_legs(
            "NVDA",
            [SelectedLeg(strike=Decimal("225.0"), option_type="call", action="buy")],
            expiration=date(2026, 8, 19),
            as_of=AS_OF,
        )

        assert len(quotes) == 1
        assert quotes[0].ask == Decimal("6.50")
        # Primed + exactly 1 real poll to reach ASK.
        assert route.call_count["snapshot"] == 2  # type: ignore[attr-defined]

    def test_get_quotes_for_known_contracts_waits_for_bid_closing_a_buy_leg(self):
        """Settlement/exit: a KnownContract whose original action was
        BUY must wait for BID (SELL_TO_CLOSE), not stop the moment LAST
        or ASK shows up."""
        conid = 907866760
        route = self._route(
            [
                [{"conid": conid, "31": "6.35", "86": "6.55"}],  # LAST+ASK, no BID yet
                [{"conid": conid, "31": "6.35", "86": "6.55", "84": "6.20"}],  # BID arrives
            ]
        )
        provider = _make_provider(transport=httpx.MockTransport(route))

        quotes = provider.get_quotes_for_known_contracts(
            "NVDA",
            [
                KnownContract(
                    strike=Decimal("225.0"),
                    option_type="call",
                    external_contract_id=str(conid),
                    action="buy",
                )
            ],
            date(2026, 8, 19),
            AS_OF,
        )

        assert len(quotes) == 1
        assert quotes[0].bid == Decimal("6.20")
        assert route.call_count["snapshot"] == 2  # type: ignore[attr-defined]


class TestListAvailableExpirations:
    """Options Decision Engine V3 Part C -- real multi-expiration discovery
    for the Expiration Selection Engine, distinct from get_option_chain's
    single-target selection."""

    def _route_single_month(self, request: httpx.Request) -> httpx.Response:
        """AUG26 alone lists three real expirations (08-17, 08-19, 08-21)
        for the probed 225 strike -- enough to test filtering/sorting/
        capping without a second month."""
        path = request.url.path
        params = dict(httpx.QueryParams(request.url.query))
        if path == "/v1/api/iserver/auth/status":
            return httpx.Response(200, json=REAL_AUTH_STATUS)
        if path == "/v1/api/iserver/secdef/search":
            return httpx.Response(200, json=REAL_SECDEF_SEARCH_NVDA)
        if path == "/v1/api/iserver/secdef/strikes":
            return httpx.Response(200, json=REAL_STRIKES_AUG26)
        if path == "/v1/api/iserver/secdef/info" and params.get("strike") == "225.0":
            return httpx.Response(200, json=REAL_SECDEF_INFO_225_CALL)
        if path == "/v1/api/iserver/marketdata/snapshot":
            return httpx.Response(200, json=REAL_UNDERLYING_SNAPSHOT)
        raise AssertionError(f"unexpected request in test: {request.url}")

    def test_filters_dedupes_sorts_and_caps(self):
        provider = _make_provider(transport=httpx.MockTransport(self._route_single_month))

        expirations = provider.list_available_expirations(
            "NVDA", after=date(2026, 8, 17), max_candidates=5
        )

        # 08-17 excluded (not strictly after); 08-19/08-21 real and sorted.
        assert expirations == [date(2026, 8, 19), date(2026, 8, 21)]

    def test_caps_at_max_candidates(self):
        provider = _make_provider(transport=httpx.MockTransport(self._route_single_month))

        expirations = provider.list_available_expirations(
            "NVDA", after=date(2026, 8, 17), max_candidates=1
        )

        assert expirations == [date(2026, 8, 19)]

    def test_walks_a_second_month_when_first_has_too_few(self):
        """AUG26 alone yields only one real candidate after the reference
        date; must continue into SEP26 to satisfy max_candidates=3 rather
        than stopping short."""

        def route(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            params = dict(httpx.QueryParams(request.url.query))
            if path == "/v1/api/iserver/auth/status":
                return httpx.Response(200, json=REAL_AUTH_STATUS)
            if path == "/v1/api/iserver/secdef/search":
                return httpx.Response(200, json=REAL_SECDEF_SEARCH_NVDA)
            if path == "/v1/api/iserver/secdef/strikes":
                return httpx.Response(200, json=REAL_STRIKES_AUG26)
            if path == "/v1/api/iserver/secdef/info" and params.get("month") == "AUG26":
                return httpx.Response(
                    200,
                    json=[{"conid": 1, "strike": 225.0, "right": "C", "maturityDate": "20260821"}],
                )
            if path == "/v1/api/iserver/secdef/info" and params.get("month") == "SEP26":
                return httpx.Response(
                    200,
                    json=[
                        {"conid": 2, "strike": 225.0, "right": "C", "maturityDate": "20260904"},
                        {"conid": 3, "strike": 225.0, "right": "C", "maturityDate": "20260918"},
                    ],
                )
            if path == "/v1/api/iserver/marketdata/snapshot":
                return httpx.Response(200, json=REAL_UNDERLYING_SNAPSHOT)
            raise AssertionError(f"unexpected request in test: {request.url}")

        provider = _make_provider(transport=httpx.MockTransport(route))

        expirations = provider.list_available_expirations(
            "NVDA", after=date(2026, 8, 17), max_candidates=3
        )

        assert expirations == [date(2026, 8, 21), date(2026, 9, 4), date(2026, 9, 18)]

    def test_returns_empty_when_no_underlying_price(self):
        def route(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/api/iserver/auth/status":
                return httpx.Response(200, json=REAL_AUTH_STATUS)
            if request.url.path == "/v1/api/iserver/secdef/search":
                return httpx.Response(200, json=REAL_SECDEF_SEARCH_NVDA)
            if request.url.path == "/v1/api/iserver/marketdata/snapshot":
                return httpx.Response(200, json=[{"conid": 4815747}])
            raise AssertionError(f"unexpected request in test: {request.url}")

        provider = _make_provider(transport=httpx.MockTransport(route))

        assert provider.list_available_expirations("NVDA", after=date(2026, 8, 17)) == []


class TestGetQuotesForKnownContracts:
    """Phase 4.5 -- re-quoting already-identified contracts by conid,
    skipping strike/ATM discovery entirely (unlike get_option_chain).
    Reuses the exact same real-captured snapshot fixtures as
    TestGetOptionChainFullFlow, since it's the same underlying /iserver/
    marketdata/snapshot mechanism -- only the discovery step differs."""

    def _route(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        params = dict(httpx.QueryParams(request.url.query))
        if path == "/v1/api/iserver/auth/status":
            return httpx.Response(200, json=REAL_AUTH_STATUS)
        if path == "/v1/api/iserver/marketdata/snapshot":
            conids = params["conids"].split(",")
            rows = []
            for conid_str in conids:
                if int(conid_str) == 907866760:
                    rows.append(REAL_225_CALL_SNAPSHOT)
                elif int(conid_str) == 907867812:
                    rows.append(REAL_225_PUT_SNAPSHOT)
                else:
                    rows.append({"conid": int(conid_str), "conidEx": conid_str})
            return httpx.Response(200, json=rows)
        raise AssertionError(f"unexpected request in test: {request.url}")

    def _provider(self) -> IBKROptionsProvider:
        return _make_provider(transport=httpx.MockTransport(self._route))

    def test_requotes_by_known_conid_never_rediscovers_strikes(self):
        """No secdef/search, secdef/strikes, or secdef/info request is
        routed in this test's transport at all -- if the implementation
        ever fell back to discovery, this test would fail with an
        unrouted-request AssertionError, not silently pass."""
        provider = self._provider()
        contracts = [
            KnownContract(
                strike=Decimal("225.0"), option_type="call", external_contract_id="907866760"
            ),
            KnownContract(
                strike=Decimal("225.0"), option_type="put", external_contract_id="907867812"
            ),
        ]

        quotes = provider.get_quotes_for_known_contracts(
            "NVDA", contracts, date(2026, 8, 19), AS_OF
        )

        assert len(quotes) == 2
        call = next(q for q in quotes if q.option_type == "call")
        assert call.bid == Decimal("2.41")
        assert call.ask == Decimal("2.44")
        assert call.external_contract_id == "907866760"
        assert call.strike == Decimal("225.0")
        put = next(q for q in quotes if q.option_type == "put")
        assert put.bid == Decimal("1.88")
        assert put.ask == Decimal("1.89")

    def test_malformed_contract_id_is_skipped_not_raised(self):
        provider = self._provider()
        contracts = [
            KnownContract(
                strike=Decimal("225.0"), option_type="call", external_contract_id="not-a-number"
            ),
        ]

        assert (
            provider.get_quotes_for_known_contracts("NVDA", contracts, date(2026, 8, 19), AS_OF)
            == []
        )

    def test_empty_contract_list_returns_empty(self):
        provider = self._provider()

        assert provider.get_quotes_for_known_contracts("NVDA", [], date(2026, 8, 19), AS_OF) == []
