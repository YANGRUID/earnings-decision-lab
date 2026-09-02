"""Abstract provider interfaces.

Every external data source (market data, options, earnings, filings,
transcripts) is accessed only through one of these. Application code depends
on the interface, never on a concrete provider — swapping e.g. Stooq for a
paid market-data vendor means writing one new adapter, not touching callers.
See docs/data_sources.md for which concrete providers are wired up today and
why.
"""

from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal

from providers.ibkr_historical import HistoricalBar
from providers.types import (
    ConsensusEstimate,
    EarningsCalendarEntry,
    EarningsEstimatePeriod,
    FilingMetadata,
    FinnhubCalendarEntry,
    FinnhubCompanyProfile,
    KnownContract,
    OHLCBar,
    OptionQuote,
    SelectedLeg,
    SnapshotAttempt,
    TranscriptDocument,
    UnderlyingQuote,
    UpcomingEarningsCalendarEntry,
)


class MarketDataProvider(ABC):
    @abstractmethod
    def get_daily_bars(self, ticker: str, start: date, end: date) -> list[OHLCBar]:
        """Daily OHLCV bars for ``ticker`` in ``[start, end]``, inclusive."""


class OptionsDataProvider(ABC):
    @abstractmethod
    def get_option_chain(
        self,
        ticker: str,
        as_of: datetime,
        expiration: date | None = None,
        reference_date: date | None = None,
        earnings_anchored: bool = True,
    ) -> list[OptionQuote]:
        """Full (or single-expiration) option chain quoted at/near ``as_of``.

        Implementations must not backfill fields using information that
        postdates ``as_of`` — see the no-lookahead-bias principle in
        docs/data_model.md.

        ``reference_date`` is a hint, not a filter: for a provider that must
        bound an otherwise enormous universe of contracts (e.g. a live
        broker API where fetching every expiration/strike is impractical
        and costly -- see providers/ibkr_options.py), it's the
        forward-looking date to center contract discovery on, typically the
        earnings date being researched, so the provider can pick a real
        near-that-date expiration and a bounded ATM strike window instead of
        everything. Providers that already return a full chain (e.g. Alpha
        Vantage) ignore it.

        ``earnings_anchored`` tells a bounded provider which expiration-
        selection rule ``reference_date`` should be interpreted under:
        ``True`` (the default) means "nearest expiration strictly *after*
        reference_date" (reference_date is a real earnings date -- an
        expiration on or before it wouldn't outlive the event). ``False``
        means "nearest expiration on or after reference_date" (there is no
        earnings date; reference_date is just "now", and a same-day
        expiration is still a valid, usable data point). See
        analytics/options/implied_move.py's two selection functions.
        Providers that ignore ``reference_date`` also ignore this.
        """

    def list_available_expirations(
        self, ticker: str, after: date, max_candidates: int = 5
    ) -> list[date]:
        """Real listed expirations for ``ticker`` strictly after ``after``,
        up to ``max_candidates``, ascending. Used by the Expiration
        Selection Engine (analytics/options/expiration_selection.py) to
        compare multiple real candidates rather than accepting a single
        pre-picked one -- never invented dates.

        Default implementation for a provider that already returns a full
        chain in one call (e.g. Alpha Vantage): fetch once with no target
        expiration and derive the set from whatever expirations came back.
        A provider that must bound contract discovery to one expiration per
        call (e.g. IBKR) cannot use this default -- it overrides this
        method with real, bounded multi-expiration discovery instead. See
        providers/ibkr_options.py.
        """
        quotes = self.get_option_chain(ticker, datetime.now(UTC), reference_date=after)
        expirations = sorted({q.expiration_date for q in quotes if q.expiration_date > after})
        return expirations[:max_candidates]

    def get_underlying_quote(self, ticker: str) -> UnderlyingQuote | None:
        """A real, live quote for the underlying itself, fetched fresh at
        call time -- never a daily close, and never derived from
        OptionQuote.underlying_price (a distinct, reconstruction-only
        concept; see its docstring in providers/types.py).

        Default: unsupported (``None``). A provider with no live
        underlying capability reports unavailability honestly here;
        callers (Phase 4.4's official benchmark entry capture, see
        services/benchmark_entry_capture.py) must never substitute a
        stale price in its place. Overridden by providers that actually
        expose this -- see providers/ibkr_options.py.
        """
        return None

    def get_quotes_for_known_contracts(
        self,
        ticker: str,
        contracts: list[KnownContract],
        expiration: date,
        as_of: datetime,
        on_attempt: Callable[[SnapshotAttempt], None] | None = None,
    ) -> list[OptionQuote]:
        """Real, live quotes for contracts *already identified* (Phase
        4.5 settlement) -- deliberately not routed through get_option_
        chain()'s own strike/ATM discovery, which centers a bounded
        window on the underlying's *current* price. The underlying may
        have moved materially since these contracts were entered, and a
        fresh discovery pass could silently miss a leg that's now well
        outside that window, or -- worse -- match a different, similarly-
        struck contract instead of the exact one actually held.
        ``contracts`` are always exactly what was captured on EntrySnapshot
        at entry time; this method re-quotes those same contracts, never
        rediscovers new ones.

        ``on_attempt``, when given (IBKR execution-observability
        hardening, 2026-08-26), is called with a real ``SnapshotAttempt``
        after every real poll a provider that supports warm-up telemetry
        makes -- purely an observation hook; a provider default like this
        one, with no such polling to observe, simply never calls it.

        Default: unsupported (empty list). A provider with no stable
        per-contract identifier to re-quote by (e.g. one with no conid-
        equivalent concept) reports this honestly as "no quotes" rather
        than guessing via a fresh discovery pass; callers must never
        silently fall back to get_option_chain() in its place, since that
        would reintroduce exactly the strike-drift risk this method
        exists to avoid. Overridden by providers that actually expose a
        stable per-contract identifier -- see providers/ibkr_options.py.
        """
        return []

    def get_quotes_for_selected_legs(
        self,
        ticker: str,
        legs: list[SelectedLeg],
        expiration: date,
        as_of: datetime,
        on_attempt: Callable[[SnapshotAttempt], None] | None = None,
    ) -> list[OptionQuote]:
        """Real, live quotes for exactly the strikes/rights strategy
        generation already selected (Decision Engine entry capture --
        live market-data validation, 2026-08-26, Section 7) -- unlike
        ``get_quotes_for_known_contracts`` above, ``legs`` carry no
        provider contract identifier yet (``DecisionSnapshot.legs`` never
        persisted one; see ``SelectedLeg``'s own docstring), only the
        deterministic strike/option_type/action the ranking settled on. A
        provider that can resolve one exact contract directly from
        (symbol, expiration, strike, right) should do exactly that,
        skipping any ATM-window discovery of strikes nobody selected.

        ``on_attempt`` -- see get_quotes_for_known_contracts's own
        docstring above; the identical, optional observation hook.

        Default: delegates to ``get_option_chain`` and filters to the
        requested (strike, option_type) pairs -- correct for every
        provider (including one that already returns a full chain in one
        call, e.g. Alpha Vantage, for which there is no narrower request
        to make), just not necessarily minimal-cost. Overridden by a
        provider where a narrower real request is actually possible --
        see providers/ibkr_options.py.
        """
        quotes = self.get_option_chain(ticker, as_of, expiration=expiration)
        wanted = {(leg.strike, leg.option_type) for leg in legs}
        return [q for q in quotes if (q.strike, q.option_type) in wanted]

    # ------------------------------------------------------------------
    # Historical-close reconstruction (services/options_reconstruction.py)
    # ------------------------------------------------------------------
    #
    # IBKR TWS Migration, Phase 3 readiness -- these three methods used to
    # be called only via a concrete IBKROptionsProvider that services/
    # options_reconstruction.py constructed directly, bypassing providers/
    # factory.py entirely and hard-coupling reconstruction to the Web
    # transport. Promoted here so a TWS-backed provider can implement the
    # identical capability (see providers/ibkr_tws_options.py's own
    # overrides) without services/options_reconstruction.py needing to
    # know which concrete adapter it's holding. This is a provider-
    # resolution change only -- no reconstruction math (bar selection,
    # skew checks, Black-Scholes fallback) moves or changes.

    def resolve_expiration_for_reconstruction(
        self, ticker: str, reference_date: date, earnings_date: date | None
    ) -> date | None:
        """Which real, currently-listed expiration a historical-close
        reconstruction should target -- the same selection rule
        (select_expiration_after / select_nearest_listed_expiration) live
        collection uses, never a separate, possibly-disagreeing rule.

        No provider covered by this project can ask "what was listed as
        of a past date" -- this necessarily resolves against *today's*
        currently listed expirations, an honest approximation accurate
        for reconstructing a recent (not a far-past) close, since listed
        expirations only change by rolling off after they themselves
        expire.

        Default: unsupported (``None``). A provider with no per-
        expiration discovery capability (e.g. Alpha Vantage, which
        already returns a full chain in one call and has no separate
        reconstruction path) reports this honestly; callers must treat
        ``None`` as "reconstruction unavailable from this provider,"
        never retry with a guessed date. Overridden by providers/
        ibkr_options.py and providers/ibkr_tws_options.py.
        """
        return None

    def discover_contracts_for_expiration(
        self, ticker: str, target_expiration: date
    ) -> tuple[int, Decimal | None, list[tuple[Decimal, str, int]]]:
        """The underlying's conid, its current live price (used only to
        center the strike window -- never confused with a reconstructed
        historical underlying price), and every real ``(strike, right,
        option_conid)`` listed for ``target_expiration``, for a caller
        that already knows which expiration it wants (e.g. services/
        options_reconstruction.py, after resolve_expiration_for_
        reconstruction above has already picked one).

        Default: unsupported (conid ``0``, price ``None``, no contracts).
        Overridden by providers/ibkr_options.py and providers/ibkr_tws_
        options.py.
        """
        return 0, None, []

    def get_historical_bars(
        self,
        conid: int,
        *,
        bar: str = "1min",
        period: str = "1d",
        end_time: datetime | None = None,
        outside_rth: bool = False,
    ) -> list[HistoricalBar]:
        """Real historical OHLC bars for ``conid`` up to (and including)
        ``end_time`` -- the step after discover_contracts_for_expiration
        resolves which conids to ask for. Real bars only, never a
        fabricated/filled value (see providers/ibkr_historical.py's own
        docstring for the full contract).

        ``bar``/``period`` use this ABC's own canonical, compact
        vocabulary (``"1min"``, ``"1d"``) -- the Web adapter's native
        syntax and hence the form every caller passes. A provider whose
        real API uses different syntax (TWS's own space-separated
        ``"1 min"``/``"1 D"``) translates internally; see providers/
        ibkr_tws_options.py's own override.

        Default: unsupported (empty list). Overridden by providers/
        ibkr_options.py and providers/ibkr_tws_options.py.
        """
        return []


class EarningsDataProvider(ABC):
    @abstractmethod
    def get_earnings_calendar(self, ticker: str) -> list[EarningsCalendarEntry]:
        """Known/expected earnings dates for ``ticker``, past and upcoming."""

    @abstractmethod
    def get_consensus_estimate(
        self, ticker: str, fiscal_year: int, fiscal_quarter: int, as_of: datetime
    ) -> ConsensusEstimate | None:
        """Consensus EPS/revenue estimate as it stood at ``as_of``."""


class EarningsEstimatesProvider(ABC):
    """Analyst consensus data, keyed by fiscal period end date rather than
    fiscal_year/fiscal_quarter -- see EarningsEstimatePeriod's docstring for
    why. A separate interface from EarningsDataProvider (which already
    existed, unimplemented, since Phase 1): that one's shape assumes a known
    discrete fiscal_quarter, which doesn't hold for the one period that
    matters most in practice (the next unreported one, which for all four
    covered tickers today is the fiscal-Q4/FYE period this project has never
    modeled as a discrete quarter). Consensus data is never a substitute for
    SEC actuals -- see EarningsResult, whose source of truth stays EDGAR.
    """

    @abstractmethod
    def get_earnings_estimates(self, ticker: str) -> list[EarningsEstimatePeriod]:
        """Current consensus by fiscal period. This reflects today's
        analyst view for every period the provider returns (including past
        ones) -- not a preserved point-in-time snapshot from back then, so
        callers must only treat entries for genuinely unreported periods as
        "expectations"; see docs/data_sources.md.
        """

    @abstractmethod
    def get_next_earnings_date(self, ticker: str) -> UpcomingEarningsCalendarEntry | None:
        """The provider's own prediction of the next report date -- not
        SEC-confirmed. ``None`` if the provider has nothing upcoming.
        """


class EarningsCalendarProvider(ABC):
    """Cross-symbol, forward-looking earnings calendar (Phase 4) -- distinct
    from EarningsDataProvider and EarningsEstimatesProvider above, both of
    which are single-ticker-scoped ("this ticker's calendar" / "this
    ticker's next date"). Nothing before Phase 4 ever needed to ask "who
    reports in this date range, across the whole market" -- this is that
    question's real interface, not a retrofit of either existing one.
    """

    @abstractmethod
    def get_earnings_calendar(self, from_date: date, to_date: date) -> list[FinnhubCalendarEntry]:
        """Every real, currently-scheduled earnings event in
        ``[from_date, to_date]``, inclusive, across every symbol the
        provider covers -- never filtered to a known universe by this
        method itself; eligibility (market cap, US-listed, tradable
        options) is a separate, later concern (Phase 5)."""

    @abstractmethod
    def get_company_profile(self, symbol: str) -> FinnhubCompanyProfile | None:
        """Real name/logo/market-cap/country for ``symbol`` -- the
        calendar entries above carry none of these, only a raw symbol.
        None for an unknown/delisted symbol, never a malformed response
        (Phase 4.2, see services/earnings_calendar_sync.py)."""


class FilingsProvider(ABC):
    @abstractmethod
    def search_filings(
        self, cik: str, filing_types: list[str], limit: int = 10
    ) -> list[FilingMetadata]:
        """Most recent filings of the given types for ``cik``."""

    @abstractmethod
    def get_filing_text(self, filing: FilingMetadata) -> str:
        """Best-effort plain text of a filing's primary document.

        Phase 1 implementations may return lightly-cleaned text; full
        parsing/chunking for RAG is implemented in Phase 5.
        """


class TranscriptProvider(ABC):
    @abstractmethod
    def get_transcript(
        self, ticker: str, fiscal_year: int, fiscal_quarter: int
    ) -> TranscriptDocument | None:
        """Earnings call transcript, where legally accessible. ``None`` if unavailable."""
