"""Fixture-backed providers — TEST AND DEVELOPMENT USE ONLY.

These load canned JSON and are never wired into ingestion or the API. Their
purpose is (a) letting tests exercise code against the ``*Provider``
interfaces without live network calls, and (b) letting the rest of the
system be built against a stable shape while a real consensus-estimates /
options-data provider is selected (tracked in docs/data_sources.md — this is
a genuine, currently-open gap, not a shortcut: no paid options/estimates
vendor has been evaluated and wired up yet).

Per project convention, fabricated data is never presented as if it were
real: any UI or ingestion code path must fail or be explicitly labeled if it
would otherwise end up reading from one of these classes.
"""

import json
from datetime import date, datetime
from pathlib import Path

from providers.base import EarningsDataProvider, OptionsDataProvider, TranscriptProvider
from providers.types import (
    ConsensusEstimate,
    EarningsCalendarEntry,
    OptionQuote,
    TranscriptDocument,
)

_FIXTURES_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures"


class FixtureEarningsDataProvider(EarningsDataProvider):
    def __init__(self) -> None:
        self._calendar = json.loads((_FIXTURES_DIR / "earnings_calendar.json").read_text())
        self._estimates = json.loads((_FIXTURES_DIR / "consensus_estimates.json").read_text())

    def get_earnings_calendar(self, ticker: str) -> list[EarningsCalendarEntry]:
        return [
            EarningsCalendarEntry(**row, source_provider="fixture", retrieved_at=datetime.now())
            for row in self._calendar
            if row["ticker"] == ticker.upper()
        ]

    def get_consensus_estimate(
        self, ticker: str, fiscal_year: int, fiscal_quarter: int, as_of: datetime
    ) -> ConsensusEstimate | None:
        for row in self._estimates:
            if (
                row["ticker"] == ticker.upper()
                and row["fiscal_year"] == fiscal_year
                and row["fiscal_quarter"] == fiscal_quarter
            ):
                return ConsensusEstimate(
                    **row, source_provider="fixture", retrieved_at=datetime.now()
                )
        return None


class FixtureOptionsDataProvider(OptionsDataProvider):
    def __init__(self) -> None:
        self._chain = json.loads((_FIXTURES_DIR / "option_chain.json").read_text())

    def get_option_chain(
        self,
        ticker: str,
        as_of: datetime,
        expiration: date | None = None,
        reference_date: date | None = None,
        earnings_anchored: bool = True,
    ) -> list[OptionQuote]:
        del reference_date, earnings_anchored  # fixture already returns the full canned chain
        return [
            OptionQuote(**row, source_provider="fixture", retrieved_at=datetime.now())
            for row in self._chain
            if row["ticker"] == ticker.upper()
            and (expiration is None or row["expiration_date"] == expiration.isoformat())
        ]


class FixtureTranscriptProvider(TranscriptProvider):
    def __init__(self) -> None:
        self._transcript = json.loads((_FIXTURES_DIR / "transcript.json").read_text())

    def get_transcript(
        self, ticker: str, fiscal_year: int, fiscal_quarter: int
    ) -> TranscriptDocument | None:
        row = self._transcript
        if (
            row["ticker"] != ticker.upper()
            or row["fiscal_year"] != fiscal_year
            or row["fiscal_quarter"] != fiscal_quarter
        ):
            return None
        return TranscriptDocument(**row, source_provider="fixture", retrieved_at=datetime.now())
