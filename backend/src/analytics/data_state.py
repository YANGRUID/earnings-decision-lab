"""Derives a single ``DataState`` (models/enums.py) for a data point that
came from a real provider snapshot -- the shared vocabulary every page
must use instead of inventing its own "stale"/"old"/"cached" label. See
analytics/market_session.py for the real US-market-hours signal this
combines with real snapshot ages and real provider quality flags.
"""

from dataclasses import dataclass
from datetime import datetime

from analytics.market_session import EASTERN, MarketSession, get_market_session
from models.enums import DataState


def compute_options_data_state(
    snapshot_timestamp: datetime | None,
    market_data_quality: str | None,
    as_of: datetime,
) -> DataState:
    """``market_data_quality`` is the real per-quote flag IBKR/Alpha Vantage
    returned (live/delayed/frozen/unknown, see providers/ibkr_client.py's
    decode_market_data_quality) -- this function never guesses quality
    itself, only decides how the *age* of the snapshot relative to real
    market hours changes what that quality means to show.
    """
    if snapshot_timestamp is None:
        return DataState.NOT_COLLECTED

    same_session_day = (
        snapshot_timestamp.astimezone(EASTERN).date() == as_of.astimezone(EASTERN).date()
    )
    if not same_session_day:
        return DataState.PREVIOUS_SESSION

    market = get_market_session(as_of)
    if market.session == MarketSession.CLOSED:
        return DataState.MARKET_CLOSED

    if market_data_quality == "live":
        return DataState.LIVE
    if market_data_quality == "delayed":
        return DataState.DELAYED
    if market_data_quality == "frozen":
        return DataState.FROZEN
    # Phase 4 market-data-quality hardening (2026-08-26), Section 15 --
    # IBKR's real "N" (Not Subscribed) availability code decodes to the
    # specific "unavailable" signal (providers/ibkr_client.py's
    # decode_market_data_quality), which maps to the existing, more
    # precise PREMIUM_REQUIRED state rather than the generic UNKNOWN one.
    if market_data_quality == "unavailable":
        return DataState.PREMIUM_REQUIRED
    return DataState.UNKNOWN


@dataclass(frozen=True)
class SnapshotAge:
    minutes: int
    label: str
    """Human-readable age, e.g. "17h 24m" or "3m" -- never more precise
    than minutes (seconds-level precision would be false confidence for a
    point-in-time research snapshot)."""


def compute_snapshot_age(snapshot_timestamp: datetime, as_of: datetime) -> SnapshotAge:
    total_minutes = max(0, int((as_of - snapshot_timestamp).total_seconds() // 60))
    hours, minutes = divmod(total_minutes, 60)
    label = f"{hours}h {minutes}m" if hours else f"{minutes}m"
    return SnapshotAge(minutes=total_minutes, label=label)
