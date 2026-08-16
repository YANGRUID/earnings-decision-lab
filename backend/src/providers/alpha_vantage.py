"""Real Alpha Vantage daily OHLCV adapter — documented, authenticated API.

Used as a fallback behind Tiingo (providers.fallback.MarketDataProviderChain)
because the free tier is tight (25 requests/day, ~5/minute) — fine as an
occasional backstop, not as the primary source for six symbols' worth of
routine ingestion. See docs/data_sources.md.
"""

from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from providers.base import MarketDataProvider
from providers.types import OHLCBar

_QUERY_URL = "https://www.alphavantage.co/query"


class AlphaVantageError(Exception):
    """Raised when Alpha Vantage returns a 200 with an error/rate-limit note
    instead of a time series (its API reports failures in the JSON body,
    not via HTTP status)."""


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return False


class AlphaVantageMarketDataProvider(MarketDataProvider):
    def __init__(self, api_key: str, client: httpx.Client | None = None) -> None:
        if not api_key:
            raise ValueError("Alpha Vantage requires an API key (ALPHA_VANTAGE_API_KEY in .env)")
        self._api_key = api_key
        self._client = client or httpx.Client(timeout=15.0)

    @retry(
        retry=retry_if_exception(_retryable),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )
    def get_daily_bars(self, ticker: str, start: date, end: date) -> list[OHLCBar]:
        response = self._client.get(
            _QUERY_URL,
            params={
                "function": "TIME_SERIES_DAILY",
                "symbol": ticker.upper(),
                "outputsize": "full",
                "apikey": self._api_key,
            },
        )
        response.raise_for_status()
        payload = response.json()
        series = payload.get("Time Series (Daily)")
        if series is None:
            note = payload.get("Note") or payload.get("Information") or payload.get("Error Message")
            raise AlphaVantageError(note or f"unexpected response shape: {list(payload)}")

        retrieved_at = datetime.now(UTC)
        bars: list[OHLCBar] = []
        for date_str, values in series.items():
            trade_date = date.fromisoformat(date_str)
            if not (start <= trade_date <= end):
                continue
            bars.append(
                OHLCBar(
                    ticker=ticker.upper(),
                    trade_date=trade_date,
                    open=Decimal(values["1. open"]),
                    high=Decimal(values["2. high"]),
                    low=Decimal(values["3. low"]),
                    close=Decimal(values["4. close"]),
                    volume=int(values["5. volume"]),
                    source_provider="alpha_vantage",
                    retrieved_at=retrieved_at,
                )
            )
        return sorted(bars, key=lambda b: b.trade_date)
