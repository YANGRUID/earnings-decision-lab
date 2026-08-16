"""Stooq daily OHLCV adapter — free, no API key, CSV download.

Stooq (https://stooq.com) publishes free end-of-day price history for
personal/research use with no key or signup. It is not a licensed
redistribution source: per docs/data_sources.md, downloaded price data is
never committed to this repository (see .gitignore), only used to compute
derived analytics that are themselves stored with full provenance.
"""

import csv
import io
from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from providers.base import MarketDataProvider
from providers.types import OHLCBar

_DOWNLOAD_URL = "https://stooq.com/q/d/l/"


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return False


class StooqMarketDataProvider(MarketDataProvider):
    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client(timeout=10.0)

    @retry(
        retry=retry_if_exception(_retryable),
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
        reraise=True,
    )
    def get_daily_bars(self, ticker: str, start: date, end: date) -> list[OHLCBar]:
        symbol = f"{ticker.lower()}.us"
        params = {
            "s": symbol,
            "d1": start.strftime("%Y%m%d"),
            "d2": end.strftime("%Y%m%d"),
            "i": "d",
        }
        response = self._client.get(_DOWNLOAD_URL, params=params)
        response.raise_for_status()

        if response.text.strip().lower().startswith("no data") or not response.text.strip():
            return []

        retrieved_at = datetime.now(UTC)
        bars: list[OHLCBar] = []
        reader = csv.DictReader(io.StringIO(response.text))
        for row in reader:
            try:
                bars.append(
                    OHLCBar(
                        ticker=ticker.upper(),
                        trade_date=date.fromisoformat(row["Date"]),
                        open=Decimal(row["Open"]),
                        high=Decimal(row["High"]),
                        low=Decimal(row["Low"]),
                        close=Decimal(row["Close"]),
                        volume=int(row["Volume"]),
                        source_provider="stooq",
                        retrieved_at=retrieved_at,
                    )
                )
            except (KeyError, ValueError):
                continue
        return bars
