"""TWS API historical-bar parity for IBKROptionsProvider's existing
providers/ibkr_historical.py -- IBKR TWS Migration Phase 1, Section 38.

Deliberately reuses ``HistoricalBar`` directly rather than defining a
second, parallel type -- a caller that already knows how to consume the
Web adapter's bars (e.g. a future services/options_reconstruction.py
change) needs no new type to also consume TWS's. Only ``fetch_historical_
bars``'s own transport differs; its contract (real bars only, never a
guessed/filled value, UTC-aware timestamps -- Section 39) is identical.
"""

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from ibapi.const import UNSET_DECIMAL
from ibapi.contract import Contract

from providers.ibkr_historical import HistoricalBar
from providers.ibkr_tws_client import TWSConnectionManager


def _decimal_or_none(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _bar_volume_or_none(raw_bar: object) -> Decimal | None:
    """IBKR TWS Migration Phase 1.1, Section 4 -- BarData.volume defaults
    to the official client's real UNSET_DECIMAL sentinel (2**127-1) when
    genuinely absent, not None or 0 -- confirmed directly against the
    installed ibapi package. Recording it as-is would silently report a
    fabricated, absurd volume; this is the historical-bars equivalent of
    the -1 "no data" convention this project's own price-tick handling
    already respects."""
    volume = _decimal_or_none(getattr(raw_bar, "volume", None))
    if volume == UNSET_DECIMAL:
        return None
    return volume


def fetch_historical_bars(
    connection: TWSConnectionManager,
    conid: int,
    *,
    bar: str = "1 min",
    period: str = "1 D",
    end_time: datetime | None = None,
    outside_rth: bool = False,
) -> list[HistoricalBar]:
    """Real historical bars for ``conid`` up to (and including) ``end_time``
    (UTC) -- same real-only, no-fabrication contract as providers/ibkr_
    historical.py::fetch_historical_bars. ``bar``/``period`` use TWS's own
    space-separated vocabulary ("1 min", "1 D") rather than the Web
    adapter's compact one ("1min", "1d") -- a real, disclosed difference
    between the two APIs' own request syntax (see this migration's Phase 1
    report, Section K); callers pass whichever their target provider
    expects.
    """
    # A bare conId + exchange is enough to identify the contract (no
    # secType/currency needed -- confirmed against ibapi's own Contract
    # semantics); this path is only ever reached with an already-known-
    # valid conid, resolved earlier by the caller.
    contract = Contract()
    contract.conId = conid
    contract.exchange = "SMART"
    end_str = (end_time.astimezone(UTC) if end_time else datetime.now(UTC)).strftime(
        "%Y%m%d-%H:%M:%S"
    )

    raw_bars = connection.request_historical_bars(
        contract,
        end_datetime=end_str,
        duration=period,
        bar_size=bar,
        what_to_show="TRADES",
        use_rth=not outside_rth,
    )

    bars: list[HistoricalBar] = []
    for raw in raw_bars:
        o = _decimal_or_none(getattr(raw, "open", None))
        h = _decimal_or_none(getattr(raw, "high", None))
        low = _decimal_or_none(getattr(raw, "low", None))
        c = _decimal_or_none(getattr(raw, "close", None))
        raw_date = getattr(raw, "date", None)
        if raw_date is None or o is None or h is None or low is None or c is None:
            continue
        try:
            timestamp = datetime.fromtimestamp(int(raw_date), tz=UTC)
        except (TypeError, ValueError):
            continue
        bars.append(
            HistoricalBar(
                timestamp=timestamp,
                open=o,
                high=h,
                low=low,
                close=c,
                volume=_bar_volume_or_none(raw),
            )
        )
    return bars
