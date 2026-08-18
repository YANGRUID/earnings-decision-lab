"""Real US equities market-session awareness (Eastern time, weekday-aware)
-- never inferred from "no quotes came back" (a provider being empty and
the market being closed are different, independently real conditions; see
services/options_analytics.py and the AMD bug this distinction was born
from).

Known, honest limitation: this does NOT know about NYSE/Nasdaq market
holidays (Thanksgiving, Christmas, etc.) -- there is no real holiday
calendar wired up. A holiday reads as REGULAR/PRE_MARKET/AFTER_HOURS by
time-of-day alone when it should read CLOSED. Documented here rather than
silently wrong: never hard-code a specific timezone's local time (e.g.
Swiss time) as a stand-in for US market hours -- always real US Eastern
time via zoneinfo.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")

_PRE_MARKET_OPEN = time(4, 0)
_REGULAR_OPEN = time(9, 30)
_REGULAR_CLOSE = time(16, 0)
_AFTER_HOURS_CLOSE = time(20, 0)


class MarketSession(StrEnum):
    PRE_MARKET = "pre_market"
    REGULAR = "regular"
    AFTER_HOURS = "after_hours"
    CLOSED = "closed"


@dataclass(frozen=True)
class MarketSessionStatus:
    session: MarketSession
    as_of_eastern: datetime
    opens_in_minutes: int | None
    """Minutes until the next regular-session open -- only set when
    ``session`` is CLOSED. Weekday-aware but not holiday-aware (see module
    docstring); a long weekend around a market holiday will under-report
    this by however many holiday days fall in between.
    """


def _is_weekday(d: datetime) -> bool:
    return d.weekday() < 5  # Mon=0 .. Fri=4


def _next_regular_open(now_eastern: datetime) -> datetime:
    candidate_date = now_eastern.date()
    if not (_is_weekday(now_eastern) and now_eastern.time() < _REGULAR_OPEN):
        candidate_date += timedelta(days=1)
    candidate = datetime.combine(candidate_date, _REGULAR_OPEN, tzinfo=EASTERN)
    while not _is_weekday(candidate):
        candidate += timedelta(days=1)
    return candidate


def get_market_session(as_of_utc: datetime | None = None) -> MarketSessionStatus:
    now_eastern = (as_of_utc or datetime.now(UTC)).astimezone(EASTERN)

    if not _is_weekday(now_eastern):
        session = MarketSession.CLOSED
    else:
        t = now_eastern.time()
        if _PRE_MARKET_OPEN <= t < _REGULAR_OPEN:
            session = MarketSession.PRE_MARKET
        elif _REGULAR_OPEN <= t < _REGULAR_CLOSE:
            session = MarketSession.REGULAR
        elif _REGULAR_CLOSE <= t < _AFTER_HOURS_CLOSE:
            session = MarketSession.AFTER_HOURS
        else:
            session = MarketSession.CLOSED

    opens_in_minutes = None
    if session == MarketSession.CLOSED:
        next_open = _next_regular_open(now_eastern)
        opens_in_minutes = int((next_open - now_eastern).total_seconds() // 60)

    return MarketSessionStatus(
        session=session, as_of_eastern=now_eastern, opens_in_minutes=opens_in_minutes
    )
