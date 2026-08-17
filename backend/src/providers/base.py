"""Abstract provider interfaces.

Every external data source (market data, options, earnings, filings,
transcripts) is accessed only through one of these. Application code depends
on the interface, never on a concrete provider — swapping e.g. Stooq for a
paid market-data vendor means writing one new adapter, not touching callers.
See docs/data_sources.md for which concrete providers are wired up today and
why.
"""

from abc import ABC, abstractmethod
from datetime import date, datetime

from providers.types import (
    ConsensusEstimate,
    EarningsCalendarEntry,
    EarningsEstimatePeriod,
    FilingMetadata,
    OHLCBar,
    OptionQuote,
    TranscriptDocument,
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
        """


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
