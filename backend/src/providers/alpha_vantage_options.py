"""Real Alpha Vantage options-chain adapter for REALTIME_OPTIONS (and the
same parsing/detection logic for HISTORICAL_OPTIONS, which shares REALTIME_OPTIONS'
per-contract schema per Alpha Vantage's docs).

**Confirmed live during Phase 12: both endpoints require a premium Alpha
Vantage subscription this project's current plan does not have.**
REALTIME_OPTIONS returns HTTP 200 with an explicit
``"message": "This is a premium endpoint..."`` field and artificial sample
data (fake contract IDs like "XXYYZZ999999C00020000", an invalid expiration
"2099-99-99") -- this adapter treats that shape as an error, never as real
data, so a caller can never receive fabricated contracts. HISTORICAL_OPTIONS
returns an even more explicit ``{"Information": "... premium ..."}`` body
with no data at all.

The real-data parsing path below (field names, ``require_greeks=true``)
follows Alpha Vantage's current published documentation for this endpoint's
schema, but is **unverified against a real (non-demo) response** -- this
project has no subscription that returns one. If/when a real subscription
is available, re-verify field names against an actual response before
trusting ingested Greeks. See docs/data_sources.md.
"""

from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from observability.http_client import new_http_client
from providers.alpha_vantage import AlphaVantageError
from providers.base import OptionsDataProvider
from providers.types import OptionQuote

_QUERY_URL = "https://www.alphavantage.co/query"
_PREMIUM_MESSAGE_PREFIX = "This is a premium endpoint"


class PremiumEndpointRequiredError(AlphaVantageError):
    """Raised when Alpha Vantage's own response says this endpoint needs a
    subscription tier this project's configured key doesn't have -- never
    silently swallowed, never treated as "no data", since those mean
    different things (a real empty chain vs. a plan limitation)."""


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return False


def _decimal_or_none(value) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


class AlphaVantageOptionsProvider(OptionsDataProvider):
    def __init__(self, api_key: str, client: httpx.Client | None = None) -> None:
        if not api_key:
            raise ValueError("Alpha Vantage requires an API key (ALPHA_VANTAGE_API_KEY in .env)")
        self._api_key = api_key
        self._client = client or new_http_client(timeout=20.0)

    @retry(
        retry=retry_if_exception(_retryable),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )
    def get_option_chain(
        self,
        ticker: str,
        as_of: datetime,
        expiration: date | None = None,
        reference_date: date | None = None,
    ) -> list[OptionQuote]:
        # reference_date is a bounding hint for providers that can't return
        # a full chain -- irrelevant here, since this already fetches every
        # expiration/strike in one call.
        del reference_date
        params = {
            "function": "REALTIME_OPTIONS",
            "symbol": ticker.upper(),
            "require_greeks": "true",
            "apikey": self._api_key,
        }
        if expiration is not None:
            params["contract"] = expiration.isoformat()

        response = self._client.get(_QUERY_URL, params=params)
        response.raise_for_status()
        payload = response.json()

        message = payload.get("message", "")
        if isinstance(message, str) and message.startswith(_PREMIUM_MESSAGE_PREFIX):
            raise PremiumEndpointRequiredError(
                f"REALTIME_OPTIONS requires a premium Alpha Vantage plan: {message}"
            )

        contracts = payload.get("data")
        if contracts is None:
            note = payload.get("Note") or payload.get("Information") or payload.get("Error Message")
            raise AlphaVantageError(note or f"unexpected response shape: {list(payload)}")

        retrieved_at = datetime.now(UTC)
        quotes: list[OptionQuote] = []
        for contract in contracts:
            try:
                contract_expiration = date.fromisoformat(contract["expiration"])
                strike = Decimal(str(contract["strike"]))
                option_type = contract["type"].lower()
            except (KeyError, ValueError, InvalidOperation):
                continue
            if option_type not in ("call", "put"):
                continue

            quotes.append(
                OptionQuote(
                    ticker=ticker.upper(),
                    snapshot_timestamp=as_of,
                    expiration_date=contract_expiration,
                    strike=strike,
                    option_type=option_type,
                    bid=_decimal_or_none(contract.get("bid")),
                    ask=_decimal_or_none(contract.get("ask")),
                    last_price=_decimal_or_none(contract.get("last")),
                    volume=int(contract["volume"])
                    if contract.get("volume") not in (None, "")
                    else None,
                    open_interest=int(contract["open_interest"])
                    if contract.get("open_interest") not in (None, "")
                    else None,
                    implied_volatility=_decimal_or_none(contract.get("implied_volatility")),
                    delta=_decimal_or_none(contract.get("delta")),
                    gamma=_decimal_or_none(contract.get("gamma")),
                    theta=_decimal_or_none(contract.get("theta")),
                    vega=_decimal_or_none(contract.get("vega")),
                    source_provider="alpha_vantage",
                    retrieved_at=retrieved_at,
                )
            )
        return quotes
