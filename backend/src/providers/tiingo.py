"""Real Tiingo daily OHLCV adapter — documented, authenticated API (not a
scrape), free tier: 500 requests/hour, ~20+ years of history per symbol in
one call. See docs/data_sources.md for why this replaced Stooq/Yahoo.
"""

from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from providers.base import MarketDataProvider
from providers.types import OHLCBar

_PRICES_URL = "https://api.tiingo.com/tiingo/daily/{ticker}/prices"


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return False


class TiingoMarketDataProvider(MarketDataProvider):
    def __init__(self, api_key: str, client: httpx.Client | None = None) -> None:
        if not api_key:
            raise ValueError("Tiingo requires an API key (TIINGO_API_KEY in .env)")
        self._api_key = api_key
        self._client = client or httpx.Client(timeout=15.0)

    @retry(
        retry=retry_if_exception(_retryable),
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
        reraise=True,
    )
    def get_daily_bars(self, ticker: str, start: date, end: date) -> list[OHLCBar]:
        response = self._client.get(
            _PRICES_URL.format(ticker=ticker.lower()),
            params={
                "startDate": start.isoformat(),
                "endDate": end.isoformat(),
                "token": self._api_key,
                "format": "json",
            },
        )
        response.raise_for_status()
        retrieved_at = datetime.now(UTC)
        bars: list[OHLCBar] = []
        for row in response.json():
            bars.append(
                OHLCBar(
                    ticker=ticker.upper(),
                    trade_date=date.fromisoformat(row["date"][:10]),
                    open=Decimal(str(row["open"])),
                    high=Decimal(str(row["high"])),
                    low=Decimal(str(row["low"])),
                    close=Decimal(str(row["close"])),
                    volume=int(row["volume"]),
                    source_provider="tiingo",
                    retrieved_at=retrieved_at,
                )
            )
        return bars
