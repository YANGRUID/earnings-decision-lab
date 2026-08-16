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
    FilingMetadata,
    OHLCBar,
    OptionQuote,
    TranscriptDocument,
)


class MarketDataProvider(ABC):
    @abstractmethod
    def get_daily_bars(self, ticker: str, start: date, end: date) -> list[OHLCBar]:
        """Daily OHLCV bars for ``ticker`` in ``[start, end]``, inclusive."""


class OptionsDataProvider(ABC):
    @abstractmethod
    def get_option_chain(
        self, ticker: str, as_of: datetime, expiration: date | None = None
    ) -> list[OptionQuote]:
        """Full (or single-expiration) option chain quoted at/near ``as_of``.

        Implementations must not backfill fields using information that
        postdates ``as_of`` — see the no-lookahead-bias principle in
        docs/data_model.md.
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
