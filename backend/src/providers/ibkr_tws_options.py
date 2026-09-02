"""Real IB Gateway / TWS API options-chain adapter -- IBKR TWS Migration
Phase 1. The TWS-transport sibling of providers/ibkr_options.py (the
existing Client Portal Gateway adapter, UNCHANGED by this migration): same
``OptionsDataProvider`` methods, same normalized ``OptionQuote``/
``UnderlyingQuote`` output, same broad-discovery-vs-narrow-acquisition
split (``get_option_chain`` vs. ``get_quotes_for_known_contracts``/
``get_quotes_for_selected_legs``) -- but reached over TWS's own socket API
via providers/ibkr_tws_client.py instead of Gateway REST calls.

Two real, disclosed architectural differences from the Web adapter (see
this migration's Phase 1 report, Section G, for the full writeup):

1. Expiration/strike discovery is ONE call (``reqSecDefOptParams``) that
   already returns the COMPLETE listed expiration/strike set for the
   underlying -- no per-month-window walking loop is needed the way
   providers/ibkr_options.py's own ``_resolve_target_expiration``/
   ``list_available_expirations`` require for the Web Gateway's narrower
   ``/iserver/secdef/strikes`` (one month at a time). ``STRIKES_AROUND_ATM``
   is still applied client-side, same as the Web adapter and same
   real reason: a bounded research window, not a discovery limitation
   (see analytics/decision/v4_chain_coverage.py's own real audit of this
   exact distinction on the Web side).
2. Market data is requested per-CONTRACT (one ``reqMktData`` call = one
   contract), not per-BATCH the way the Web Gateway's
   ``/iserver/marketdata/snapshot?conids=1,2,3`` accepts many conids in
   one call. A multi-leg strategy's quotes are therefore resolved with
   one bounded warm-up loop per leg, sequentially -- simpler and correct
   for Phase 1's real leg counts (1-4), and each leg still has its own
   real timeout bound (Section 25); true concurrent per-leg subscriptions
   are a real, disclosed Phase 2+ optimization, not built here.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

from ibapi.contract import Contract

from analytics.options.implied_move import select_expiration_after, select_nearest_listed_expiration
from models.enums import QuoteRequirement
from providers.base import OptionsDataProvider
from providers.ibkr_historical import HistoricalBar
from providers.ibkr_options import IBKRContractNotFoundError
from providers.ibkr_tws_client import TWSConnectionManager
from providers.ibkr_tws_historical import fetch_historical_bars as _fetch_historical_bars_tws
from providers.types import (
    KnownContract,
    OptionQuote,
    SelectedLeg,
    SnapshotAttempt,
    SnapshotFieldPresence,
    UnderlyingQuote,
    entry_requirement_for_action,
    exit_requirement_for_action,
)

# Identical value and identical real reason as providers/ibkr_options.py's
# own STRIKES_AROUND_ATM -- a bounded research window sized for earnings-
# move analytics, applied client-side after a real, complete strike set
# is already in hand (see this module's own docstring, point 1).
STRIKES_AROUND_ATM = 5

# Requested once per option snapshot (Section 21): 100=option volume,
# 101=option open interest, 106=option implied volatility. Real per-leg
# Greeks (delta/gamma/theta/vega) arrive automatically via
# tickOptionComputation for any option contract's market-data
# subscription, independent of this generic-tick list.
_OPTION_GENERIC_TICKS = "100,101,106"

_RIGHT_BY_OPTION_TYPE = {"call": "C", "put": "P"}
_OPTION_TYPE_BY_RIGHT = {"C": "call", "P": "put"}

# get_historical_bars's own real vocabulary translation -- see that
# method's docstring. Only the values services/options_reconstruction.py
# actually passes (bar="1min", period="1d", both the OptionsDataProvider
# ABC's own defaults) are covered.
_BAR_SIZE_TO_TWS = {"1min": "1 min"}
_PERIOD_TO_TWS = {"1d": "1 D"}


def _to_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, float) and value != value:  # NaN
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _to_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value).split(".")[0])
    except (TypeError, ValueError):
        return None


def _parse_ibkr_date(value: str) -> date | None:
    text = value.strip()
    if len(text) < 8:
        return None
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
    except ValueError:
        return None


def _requirement_satisfied(result: dict, requirement: QuoteRequirement) -> bool:
    """Identical rule to providers/ibkr_options.py's own
    ``_quote_requirement_satisfied`` -- the real readiness test a bounded
    warm-up loop polls against, per contract."""
    if requirement == QuoteRequirement.ASK:
        return result.get("ask") is not None
    if requirement == QuoteRequirement.BID:
        return result.get("bid") is not None
    if requirement == QuoteRequirement.BID_ASK:
        return result.get("bid") is not None and result.get("ask") is not None
    return result.get("last") is not None  # ANALYTICAL


def ibkr_symbol(canonical: str) -> str:
    """The ONE place a canonical domain ticker becomes an IBKR contract
    symbol (V4 consolidation, Sections 12-13).

    IBKR identifies share classes with a SPACE, not a dot: Berkshire B is
    ``BRK B``, Brown-Forman B is ``BF B``. The rest of this application --
    Company.ticker, earnings_calendar_event.symbol, every historical record
    -- keeps the canonical dotted form (``BF.B``) that the calendar and
    filings providers use, and is never rewritten.

    Observed live on 2026-09-01: the official 15:55 ET run sent ``BF.A`` and
    ``BF.B`` verbatim, TWS answered error 200 (no security definition), and
    both tickers were recorded as ``skipped_ineligible`` -- a data-access
    failure filed as a business judgement.

    Only a single-letter class suffix is transformed. A dot anywhere else
    is left alone rather than guessed at.
    """
    symbol = canonical.strip().upper()
    root, dot, suffix = symbol.rpartition(".")
    # Exactly one dot, a non-empty root, and a single alphabetic suffix.
    # "A.B.C" is not a share class and is deliberately NOT transformed.
    if dot and root and "." not in root and len(suffix) == 1 and suffix.isalpha():
        return f"{root} {suffix}"
    return symbol


def _stock_contract(symbol: str) -> Contract:
    contract = Contract()
    contract.symbol = ibkr_symbol(symbol)
    contract.secType = "STK"
    contract.exchange = "SMART"
    contract.currency = "USD"
    return contract


def _bare_stock_contract(conid: int) -> Contract:
    contract = Contract()
    contract.conId = conid
    contract.secType = "STK"
    contract.exchange = "SMART"
    contract.currency = "USD"
    return contract


def _option_contract(symbol: str, expiration: date, strike: Decimal, right: str) -> Contract:
    contract = Contract()
    contract.symbol = ibkr_symbol(symbol)
    contract.secType = "OPT"
    contract.exchange = "SMART"
    contract.currency = "USD"
    contract.lastTradeDateOrContractMonth = expiration.strftime("%Y%m%d")
    contract.strike = float(strike)
    contract.right = right
    return contract


def _bare_option_contract(conid: int) -> Contract:
    contract = Contract()
    contract.conId = conid
    contract.secType = "OPT"
    contract.exchange = "SMART"
    contract.currency = "USD"
    return contract


class IBKRTWSProvider(OptionsDataProvider):
    def __init__(
        self,
        host: str,
        port: int,
        client_id: int,
        connection: TWSConnectionManager | None = None,
    ) -> None:
        self._connection = connection or TWSConnectionManager(
            host=host, port=port, client_id=client_id
        )

    def shutdown(self) -> None:
        """IBKR TWS Migration, Phase 3 readiness (Section 5) -- a clean,
        explicit teardown for the ONE shared, long-lived instance a
        backend process owns (see providers/factory.py's own
        set_shared_tws_provider docstring) -- never called by a caller
        that merely borrowed a per-call instance the old, pre-Phase-3 way
        (that path relied on garbage collection alone, matching every
        other short-lived provider this codebase constructs). Delegates
        to TWSConnectionManager.shutdown() -- identical real disconnect
        TwsHealthProbe.shutdown() already performs for its own, separate
        connection."""
        self._connection.shutdown()

    # ------------------------------------------------------------------
    # Broad discovery
    # ------------------------------------------------------------------

    def get_option_chain(
        self,
        ticker: str,
        as_of: datetime,
        expiration: date | None = None,
        reference_date: date | None = None,
        earnings_anchored: bool = True,
    ) -> list[OptionQuote]:
        self._connection.ensure_connected()
        underlying_conid = self._resolve_underlying_conid(ticker)
        underlying_price, _quality = self._underlying_price(underlying_conid)
        if underlying_price is None:
            return []

        params = self._option_params(ticker, underlying_conid)
        if params is None:
            return []

        target_expiration: date | None
        if expiration is not None:
            target_expiration = expiration
        else:
            ref = reference_date or as_of.date()
            select = (
                select_expiration_after if earnings_anchored else select_nearest_listed_expiration
            )
            listed = {
                d for d in (_parse_ibkr_date(e) for e in params["expirations"]) if d is not None
            }
            target_expiration = select(listed, ref)
        if target_expiration is None:
            return []

        strikes = self._strikes_near_atm(params["strikes"], underlying_price)
        if not strikes:
            return []

        contracts = self._resolve_contracts(ticker, target_expiration, strikes)
        if not contracts:
            return []
        return self._fetch_quotes(ticker, contracts, target_expiration, as_of)

    def list_available_expirations(
        self, ticker: str, after: date, max_candidates: int = 5
    ) -> list[date]:
        """Real listed expirations strictly after ``after`` -- unlike
        providers/ibkr_options.py's own override, no month-walking loop is
        needed at all: ``reqSecDefOptParams`` already returns the complete
        real listed set in one call (see this module's own docstring)."""
        self._connection.ensure_connected()
        underlying_conid = self._resolve_underlying_conid(ticker)
        params = self._option_params(ticker, underlying_conid)
        if params is None:
            return []
        expirations = sorted(
            d
            for d in (_parse_ibkr_date(e) for e in params["expirations"])
            if d is not None and d > after
        )
        return expirations[:max_candidates]

    def get_underlying_quote(self, ticker: str) -> UnderlyingQuote | None:
        self._connection.ensure_connected()
        underlying_conid = self._resolve_underlying_conid(ticker)
        contract = _bare_stock_contract(underlying_conid)
        result = self._connection.request_market_data_snapshot(contract, generic_ticks="")
        price = _to_decimal(result.get("last")) or _to_decimal(result.get("close"))
        if price is None:
            return None
        now = datetime.now(UTC)
        # IBKR TWS Migration, Phase 3 readiness (Section 8) -- "ibkr_tws",
        # distinct from the Web adapter's own "ibkr_web" (see providers/
        # ibkr_options.py's own matching comment) -- see that comment for
        # the full provenance rationale.
        return UnderlyingQuote(
            ticker=ticker.upper(),
            price=price,
            bid=_to_decimal(result.get("bid")),
            ask=_to_decimal(result.get("ask")),
            timestamp=now,
            market_data_quality=result.get("market_data_quality", "unknown"),
            source_provider="ibkr_tws",
            retrieved_at=now,
        )

    # ------------------------------------------------------------------
    # Historical-close reconstruction (services/options_reconstruction.py)
    # ------------------------------------------------------------------
    #
    # IBKR TWS Migration, Phase 3 readiness -- TWS counterparts to
    # providers/ibkr_options.py's own three reconstruction methods (see
    # providers/base.py's own docstrings on each for the shared, real
    # contract every OptionsDataProvider implementation honors). Reuses
    # this class's own existing building blocks (_resolve_underlying_
    # conid, _underlying_price, _option_params, _strikes_near_atm,
    # _resolve_contracts) rather than duplicating any discovery logic --
    # identical to how get_option_chain above already composes them.

    def resolve_expiration_for_reconstruction(
        self, ticker: str, reference_date: date, earnings_date: date | None
    ) -> date | None:
        """Same real selection rule as get_option_chain's own no-explicit-
        expiration branch, applied directly against TWS's real, complete
        listed expiration set (reqSecDefOptParams already returns every
        listed month in one call -- see this module's own docstring,
        point 1) -- no per-month walking loop is needed here the way
        providers/ibkr_options.py's own override requires."""
        self._connection.ensure_connected()
        underlying_conid = self._resolve_underlying_conid(ticker)
        underlying_price, _quality = self._underlying_price(underlying_conid)
        if underlying_price is None:
            return None
        params = self._option_params(ticker, underlying_conid)
        if params is None:
            return None
        listed = {
            d for d in (_parse_ibkr_date(e) for e in params["expirations"]) if d is not None
        }
        earnings_anchored = earnings_date is not None
        select_against = earnings_date if earnings_date is not None else reference_date
        select = select_expiration_after if earnings_anchored else select_nearest_listed_expiration
        return select(listed, select_against)

    def discover_contracts_for_expiration(
        self, ticker: str, target_expiration: date
    ) -> tuple[int, Decimal | None, list[tuple[Decimal, str, int]]]:
        """Real (underlying_conid, current_underlying_price_or_None,
        [(strike, right, option_conid)]) for a caller that already knows
        which expiration it wants -- reuses the exact same underlying/
        strikes/contract-resolution flow as get_option_chain above rather
        than duplicating it, identical intent to providers/ibkr_options.py's
        own method of the same name."""
        self._connection.ensure_connected()
        underlying_conid = self._resolve_underlying_conid(ticker)
        underlying_price, _quality = self._underlying_price(underlying_conid)
        if underlying_price is None:
            return underlying_conid, None, []
        params = self._option_params(ticker, underlying_conid)
        if params is None:
            return underlying_conid, underlying_price, []
        strikes = self._strikes_near_atm(params["strikes"], underlying_price)
        if not strikes:
            return underlying_conid, underlying_price, []
        contracts = self._resolve_contracts(ticker, target_expiration, strikes)
        return underlying_conid, underlying_price, contracts

    def get_historical_bars(
        self,
        conid: int,
        *,
        bar: str = "1min",
        period: str = "1d",
        end_time: datetime | None = None,
        outside_rth: bool = False,
    ) -> list[HistoricalBar]:
        """Translates the ABC's canonical, compact vocabulary (the Web
        adapter's own native syntax) into TWS's real space-separated
        ``barSizeSetting``/``durationStr`` syntax, then delegates to
        providers/ibkr_tws_historical.py's own fetch_historical_bars.
        Only the two values services/options_reconstruction.py actually
        passes (the defaults above) are translated -- deliberately no
        guessed/invented mapping for a value this codebase has never
        used; an unrecognized value raises rather than silently
        mismapping a request."""
        try:
            tws_bar = _BAR_SIZE_TO_TWS[bar]
            tws_period = _PERIOD_TO_TWS[period]
        except KeyError as exc:
            raise ValueError(
                f"get_historical_bars: no TWS translation for bar={bar!r} period={period!r}"
            ) from exc
        return _fetch_historical_bars_tws(
            self._connection,
            conid,
            bar=tws_bar,
            period=tws_period,
            end_time=end_time,
            outside_rth=outside_rth,
        )

    # ------------------------------------------------------------------
    # Narrow acquisition (already-identified contracts/legs)
    # ------------------------------------------------------------------

    def get_quotes_for_known_contracts(
        self,
        ticker: str,
        contracts: list[KnownContract],
        expiration: date,
        as_of: datetime,
        on_attempt: Callable[[SnapshotAttempt], None] | None = None,
    ) -> list[OptionQuote]:
        self._connection.ensure_connected()
        quotes: list[OptionQuote] = []
        for known in contracts:
            conid = _to_int(known.external_contract_id)
            if conid is None:
                continue
            requirement = exit_requirement_for_action(known.action)
            result = self._snapshot_with_requirement(
                _bare_option_contract(conid), requirement, conid, on_attempt
            )
            # A real observability gap found during live parity testing
            # (2026-09-01): this used to capture ONE retrieved_at before
            # the loop and reuse it for every leg, regardless of how
            # long each leg's own bounded warm-up actually took --
            # making every multi-leg quote's timestamps identical by
            # construction, never a genuine per-leg observation time.
            # Captured here, immediately after THIS leg's own real
            # market-data result is in hand, so a caller computing
            # cross-leg temporal skew (Section 16/21) measures something
            # real, not an artifact of when the batch started.
            retrieved_at = datetime.now(UTC)
            quotes.append(
                self._to_option_quote(
                    ticker,
                    known.strike,
                    known.option_type,
                    expiration,
                    as_of,
                    retrieved_at,
                    str(conid),
                    result,
                )
            )
        return quotes

    def get_quotes_for_selected_legs(
        self,
        ticker: str,
        legs: list[SelectedLeg],
        expiration: date,
        as_of: datetime,
        on_attempt: Callable[[SnapshotAttempt], None] | None = None,
    ) -> list[OptionQuote]:
        self._connection.ensure_connected()
        quotes: list[OptionQuote] = []
        seen: set[tuple[Decimal, str]] = set()
        for leg in legs:
            key = (leg.strike, leg.option_type)
            if key in seen:
                continue
            seen.add(key)
            right = _RIGHT_BY_OPTION_TYPE.get(leg.option_type)
            if right is None:
                continue
            conid = self._resolve_exact_contract(ticker, expiration, leg.strike, right)
            if conid is None:
                continue
            requirement = entry_requirement_for_action(leg.action)
            result = self._snapshot_with_requirement(
                _bare_option_contract(conid), requirement, conid, on_attempt
            )
            # Real per-leg observation time -- see the identical fix and
            # comment in get_quotes_for_known_contracts above.
            retrieved_at = datetime.now(UTC)
            quotes.append(
                self._to_option_quote(
                    ticker,
                    leg.strike,
                    leg.option_type,
                    expiration,
                    as_of,
                    retrieved_at,
                    str(conid),
                    result,
                )
            )
        return quotes

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_underlying_conid(self, ticker: str) -> int:
        details = self._connection.request_contract_details(_stock_contract(ticker))
        if not details:
            raise IBKRContractNotFoundError(
                f"no listed underlying with an options section found for {ticker!r} via TWS"
            )
        return details[0].contract.conId

    def _underlying_price(self, conid: int) -> tuple[Decimal | None, str]:
        result = self._connection.request_market_data_snapshot(
            _bare_stock_contract(conid), generic_ticks=""
        )
        price = _to_decimal(result.get("last")) or _to_decimal(result.get("close"))
        return price, result.get("market_data_quality", "unknown")

    def _option_params(self, ticker: str, underlying_conid: int) -> dict | None:
        """A real, live-verified nuance (2026-08-31): reqSecDefOptParams
        can return dozens of groups per real underlying (39 for AAPL,
        confirmed live) -- not just one per exchange. Alongside the
        normal listed-option group (tradingClass == the ticker itself,
        e.g. "AAPL", 120 real strikes / 23 expirations), a real response
        also includes unrelated adjusted/legacy contract-series groups
        (tradingClass == "2AAPL", exactly 1 strike / 1 expiration -- a
        corporate-action-adjusted series, not a normal chain), and this
        happens on MULTIPLE exchanges including SMART itself. Filtering
        on exchange=="SMART" alone (Phase 1's original filter) could
        therefore non-deterministically select either the correct
        120-strike group or the unrelated 1-strike one, depending on
        response ordering -- confirmed live to be a real risk, not
        hypothetical. Filtering additionally on tradingClass == the
        real ticker itself (never a "2"-prefixed variant) is what this
        project's own Web adapter's equivalent selection never had to
        consider (the Web Gateway's own secdef/strikes is already
        scoped to one month, never mixes contract series) -- this is a
        genuine TWS-only concern.
        """
        rows = self._connection.request_sec_def_opt_params(ticker.upper(), "STK", underlying_conid)
        candidates = [r for r in rows if r["trading_class"] == ticker.upper()]
        if not candidates:
            return None
        smart = next((r for r in candidates if r["exchange"] == "SMART"), None)
        return smart or candidates[0]

    def _strikes_near_atm(self, strikes: set, underlying_price: Decimal) -> list[Decimal]:
        all_strikes = sorted(d for d in (_to_decimal(s) for s in strikes) if d is not None)
        if not all_strikes:
            return []
        atm = min(all_strikes, key=lambda k: abs(k - underlying_price))
        idx = all_strikes.index(atm)
        low = max(0, idx - STRIKES_AROUND_ATM)
        high = min(len(all_strikes), idx + STRIKES_AROUND_ATM + 1)
        return all_strikes[low:high]

    def _resolve_exact_contract(
        self, ticker: str, expiration: date, strike: Decimal, right: str
    ) -> int | None:
        """Real, live-discovered gap (Phase 3 market-hours validation,
        2026-09-01): unlike the Web Gateway's own ``/iserver/secdef/info``
        (providers/ibkr_options.py's own ``_resolve_exact_contract``,
        which returns an empty list -- never an exception -- for a
        strike/right that simply isn't listed at this expiration, "a
        real, occasional gap, not an error"), TWS's ``reqContractDetails``
        raises a real error 200 ("no security definition found") for the
        identical condition -- confirmed live against NVDA's real, sparse
        near-term weekly strike listing. That error maps to the same
        ``IBKRContractNotFoundError`` this class's own
        ``_resolve_underlying_conid`` deliberately raises for a genuinely
        different condition (no listed underlying at all -- see that
        exception's own docstring). Caught here, narrowly, so an
        individual missing strike/right is skipped exactly like the Web
        adapter -- never lets one sparse expiration crash an entire
        chain/leg resolution."""
        try:
            details = self._connection.request_contract_details(
                _option_contract(ticker, expiration, strike, right)
            )
        except IBKRContractNotFoundError:
            return None
        return details[0].contract.conId if details else None

    def _resolve_contracts(
        self, ticker: str, expiration: date, strikes: list[Decimal]
    ) -> list[tuple[Decimal, str, int]]:
        contracts: list[tuple[Decimal, str, int]] = []
        for strike in strikes:
            for right in ("C", "P"):
                conid = self._resolve_exact_contract(ticker, expiration, strike, right)
                if conid is not None:
                    contracts.append((strike, right, conid))
        return contracts

    def _fetch_quotes(
        self,
        ticker: str,
        contracts: list[tuple[Decimal, str, int]],
        expiration: date,
        as_of: datetime,
    ) -> list[OptionQuote]:
        quotes: list[OptionQuote] = []
        for strike, right, conid in contracts:
            result = self._snapshot_with_requirement(
                _bare_option_contract(conid), QuoteRequirement.ANALYTICAL, conid, None
            )
            retrieved_at = datetime.now(UTC)  # real per-contract observation time
            quotes.append(
                self._to_option_quote(
                    ticker,
                    strike,
                    _OPTION_TYPE_BY_RIGHT[right],
                    expiration,
                    as_of,
                    retrieved_at,
                    str(conid),
                    result,
                )
            )
        return quotes

    def _snapshot_with_requirement(
        self,
        contract: Contract,
        requirement: QuoteRequirement,
        conid: int,
        on_attempt: Callable[[SnapshotAttempt], None] | None,
    ) -> dict:
        start = time.monotonic()

        def _emit(attempt_num: int, result: dict) -> None:
            if on_attempt is None:
                return
            bid = _to_decimal(result.get("bid"))
            ask = _to_decimal(result.get("ask"))
            last = _to_decimal(result.get("last"))
            presence = SnapshotFieldPresence(
                bid_present=bid is not None,
                ask_present=ask is not None,
                last_present=last is not None,
                market_data_quality=result.get("market_data_quality", "unknown"),
                bid=bid,
                ask=ask,
                last_price=last,
            )
            on_attempt(
                SnapshotAttempt(
                    attempt=attempt_num,
                    elapsed_ms=(time.monotonic() - start) * 1000,
                    per_conid={conid: presence},
                )
            )

        return self._connection.request_market_data_with_requirement(
            contract,
            requirement_satisfied=lambda result: _requirement_satisfied(result, requirement),
            generic_ticks=_OPTION_GENERIC_TICKS,
            on_attempt=_emit,
        )

    def _to_option_quote(
        self,
        ticker: str,
        strike: Decimal,
        option_type: str,
        expiration: date,
        as_of: datetime,
        retrieved_at: datetime,
        external_contract_id: str,
        result: dict,
    ) -> OptionQuote:
        return OptionQuote(
            ticker=ticker.upper(),
            snapshot_timestamp=as_of,
            expiration_date=expiration,
            strike=strike,
            option_type=option_type,
            bid=_to_decimal(result.get("bid")),
            ask=_to_decimal(result.get("ask")),
            last_price=_to_decimal(result.get("last")),
            volume=_to_int(result.get("volume")),
            open_interest=_to_int(result.get("open_interest")),
            implied_volatility=_to_decimal(
                result.get("implied_volatility", result.get("implied_volatility_generic"))
            ),
            delta=_to_decimal(result.get("delta")),
            gamma=_to_decimal(result.get("gamma")),
            theta=_to_decimal(result.get("theta")),
            vega=_to_decimal(result.get("vega")),
            market_data_quality=result.get("market_data_quality", "unknown"),
            external_contract_id=external_contract_id,
            source_provider="ibkr_tws",
            retrieved_at=retrieved_at,
        )
