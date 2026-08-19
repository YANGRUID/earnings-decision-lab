"""Real Interactive Brokers Client Portal Gateway historical-data adapter
-- ``/iserver/marketdata/history``, used only to reconstruct a past
trading session's close-window market state when no adequate
live-captured snapshot exists (see services/options_reconstruction.py).

Confirmed live against a real authenticated Gateway (WMT, 2026-08-19):
this endpoint returns real trade-based OHLC bars for both stock and
OPTION conids on this account's entitlements, at bar sizes down to
``1min``, keyed by an epoch-millisecond timestamp (field ``t``). Passing
an explicit ``startTime`` (``YYYYMMDD-HH:mm:ss``, UTC) reliably returns
that session's bars ending at/before it -- verified by requesting the
same conid with and without an explicit startTime and confirming the bar
count and final bar shift accordingly.

The Client Portal Web API does **not** expose a documented bid/ask/
midpoint bar type (unlike the classic TWS API's ``whatToShow=BID_ASK``) --
every bar this project reads from it is a TRADE/LAST price, never
fabricated into a synthetic bid/ask. See ``pricing_source`` provenance on
``OptionsSnapshot`` and services/options_reconstruction.py, which persists
this honestly as ``historical_last`` rather than pretending it's a real
two-sided market.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from providers.ibkr_client import IBKRClient


@dataclass(frozen=True)
class HistoricalBar:
    timestamp: datetime  # UTC, start of the bar
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal | None


def _decimal_or_none(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def fetch_historical_bars(
    client: IBKRClient,
    conid: int,
    *,
    bar: str = "1min",
    period: str = "1d",
    end_time: datetime | None = None,
    outside_rth: bool = False,
) -> list[HistoricalBar]:
    """Real historical bars for ``conid`` up to (and including) ``end_time``
    (UTC). When ``end_time`` is omitted, IBKR resolves bars relative to its
    own "now" -- during pre-market/weekend/holiday this naturally becomes
    the most recently completed regular session (confirmed live), but a
    caller doing point-in-time reconstruction for a *specific* target
    session should always pass an explicit ``end_time`` rather than rely on
    that implicit behavior, since it depends on exactly when reconstruction
    happens to run relative to market hours.

    Returns real bars only -- a row missing any of t/o/h/l/c is dropped
    rather than filled with a guessed value. Never raises for "no data
    available" (an empty/thin response is a legitimate, honestly reported
    outcome for e.g. an option with no trading that session); real
    connectivity/auth failures still raise via IBKRClient.get.
    """
    params: dict[str, str] = {
        "conid": str(conid),
        "period": period,
        "bar": bar,
        "outsideRth": "true" if outside_rth else "false",
    }
    if end_time is not None:
        params["startTime"] = end_time.astimezone(UTC).strftime("%Y%m%d-%H:%M:%S")

    response = client.get("/iserver/marketdata/history", params=params).json()
    bars: list[HistoricalBar] = []
    for row in response.get("data", []):
        t = row.get("t")
        o = _decimal_or_none(row.get("o"))
        h = _decimal_or_none(row.get("h"))
        low = _decimal_or_none(row.get("l"))
        c = _decimal_or_none(row.get("c"))
        if t is None or o is None or h is None or low is None or c is None:
            continue
        bars.append(
            HistoricalBar(
                timestamp=datetime.fromtimestamp(int(t) / 1000, tz=UTC),
                open=o,
                high=h,
                low=low,
                close=c,
                volume=_decimal_or_none(row.get("v")),
            )
        )
    return bars


def last_bar_at_or_before(bars: list[HistoricalBar], cutoff: datetime) -> HistoricalBar | None:
    """The most recent bar whose start time is at or before ``cutoff`` --
    "latest real market information before the cutoff", never a bar that
    starts after it (which would be information from *after* the point in
    time being reconstructed)."""
    candidates = [b for b in bars if b.timestamp <= cutoff]
    if not candidates:
        return None
    return max(candidates, key=lambda b: b.timestamp)
