"""Phase 4 -- deterministic entry/exit timing for the AI Benchmark
Portfolio's forward-tested decisions. Pure domain logic: no DB, no
network, no provider calls. This module exists to make one rule
impossible to get wrong silently: a decision must always be generated
(and its entry option prices captured) from data that existed *before*
the real market had a chance to react to the earnings release, never
after -- see ARCHITECTURE_REVIEW_PHASE4.md section 4.3 for the reasoning
this module resolves.

Three cases, matching the earnings session:

- AFTER_MARKET (AMC): the release happens after the close, so entering
  at 15:55 ET *on the earnings date itself* is still pre-release --
  valid. Exit is the next real trading day's 15:55 ET, after the market
  has had one full session to react.
- BEFORE_MARKET (BMO): the release happens before the *next* open, so by
  the time 15:55 ET on the earnings date arrives, the reaction has
  already happened. Entry must be the previous real trading day's 15:55
  ET instead. Exit is the earnings date itself (the first full session
  after the release).
- Anything else (UNKNOWN, or a session value this module doesn't
  recognize) -- conservative: treated exactly like BMO. Never assume
  AMC; assuming AMC on a real BMO event would be a real look-ahead-bias
  violation, while assuming BMO on a real AMC event only costs one extra
  day of safety margin.

Trading-day awareness here is a real improvement over
analytics/market_session.py's own documented gap ("this does NOT know
about NYSE/Nasdaq market holidays") -- that module is about *live*
session status (today, right now) where a stale holiday table is a
minor cosmetic miss; this module decides which date a permanent,
immutable, forward-tested decision snapshot gets frozen on, where a
holiday miscalculated as a trading day would be a real, unrecoverable
correctness bug (entering "on" a day the market was never open, or
skipping a real trading day when computing "previous"/"next"). Reuses
EASTERN from market_session.py rather than redefining the timezone.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from analytics.market_session import EASTERN
from models.enums import AnnouncementTime

ENTRY_EXIT_TIME = time(15, 55)


def _nth_weekday_of_month(year: int, month: int, weekday: int, n: int) -> date:
    """The nth occurrence (1-indexed) of ``weekday`` (Mon=0..Sun=6) in
    ``month``/``year`` -- e.g. n=3, weekday=0 (Monday) for MLK Day."""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return date(year, month, 1 + offset + (n - 1) * 7)


def _last_weekday_of_month(year: int, month: int, weekday: int) -> date:
    next_month_first = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    last_day = next_month_first - timedelta(days=1)
    offset = (last_day.weekday() - weekday) % 7
    return last_day - timedelta(days=offset)


def _easter_sunday(year: int) -> date:
    """Anonymous Gregorian algorithm (Meeus/Jones/Butcher) -- needed for
    Good Friday, which the NYSE closes for but which has no fixed-date or
    simple nth-weekday rule."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    ll = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ll) // 451
    month = (h + ll - 7 * m + 114) // 31
    day = ((h + ll - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _observed(d: date) -> date:
    """NYSE's standard weekend-observance shift: Saturday -> the
    preceding Friday, Sunday -> the following Monday. Known,
    intentional simplification: the NYSE's real published calendar has
    a handful of documented edge-case exceptions to this rule (most
    notably: New Year's Day is NOT shifted to the preceding Friday when
    January 1st falls on a Saturday -- NYSE simply has no holiday that
    year, to avoid closing the final trading day of the prior year).
    That specific exception is not modeled here; the standard shift is
    applied uniformly instead. Every holiday below except New Year's,
    Juneteenth, Independence Day, and Christmas is already defined by a
    weekday rule (nth Monday, 4th Thursday, Good Friday) and can never
    land on a weekend in the first place, so this only ever actually
    moves one of those four fixed-date holidays."""
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def us_market_holidays(year: int) -> set[date]:
    """The 10 NYSE-observed holidays for ``year``, weekend-shifted per
    ``_observed``. Computed, not a hardcoded table that goes stale --
    correct for any real or future year, not just ones someone
    remembered to add to a list."""
    easter = _easter_sunday(year)
    good_friday = easter - timedelta(days=2)
    raw = (
        date(year, 1, 1),  # New Year's Day
        _nth_weekday_of_month(year, 1, 0, 3),  # Martin Luther King Jr. Day
        _nth_weekday_of_month(year, 2, 0, 3),  # Washington's Birthday
        good_friday,
        _last_weekday_of_month(year, 5, 0),  # Memorial Day
        date(year, 6, 19),  # Juneteenth National Independence Day
        date(year, 7, 4),  # Independence Day
        _nth_weekday_of_month(year, 9, 0, 1),  # Labor Day
        _nth_weekday_of_month(year, 11, 3, 4),  # Thanksgiving Day
        date(year, 12, 25),  # Christmas Day
    )
    return {_observed(d) for d in raw}


def is_trading_day(d: date) -> bool:
    """Real NYSE/Nasdaq weekday, not a market holiday. Looks up both
    ``d.year`` and neighboring years' holiday sets when near a year
    boundary would otherwise be wrong -- callers only ever pass one
    date, so computing just ``us_market_holidays(d.year)`` is correct
    for every real case (no holiday ever falls in a different calendar
    year than the date being checked)."""
    if d.weekday() >= 5:
        return False
    return d not in us_market_holidays(d.year)


def previous_trading_day(d: date) -> date:
    """The most recent real trading day strictly before ``d`` -- always
    moves at least one day back, even when ``d`` itself is a trading
    day."""
    candidate = d - timedelta(days=1)
    while not is_trading_day(candidate):
        candidate -= timedelta(days=1)
    return candidate


def next_trading_day(d: date) -> date:
    """The next real trading day strictly after ``d`` -- always moves at
    least one day forward, even when ``d`` itself is a trading day."""
    candidate = d + timedelta(days=1)
    while not is_trading_day(candidate):
        candidate += timedelta(days=1)
    return candidate


def _nearest_trading_day_on_or_before(d: date) -> date:
    return d if is_trading_day(d) else previous_trading_day(d)


def _nearest_trading_day_on_or_after(d: date) -> date:
    return d if is_trading_day(d) else next_trading_day(d)


@dataclass(frozen=True)
class EarningsEntryExitSchedule:
    """The complete, deterministic answer to "when does this earnings
    event's benchmark decision get frozen, and when does it get
    settled" -- everything downstream (the scheduler, entry capture,
    exit capture) reads this and nothing else; no caller re-derives
    timing on its own."""

    earnings_date: date
    session: AnnouncementTime
    decision_generation_date: date
    entry_timestamp: datetime
    exit_date: date
    exit_timestamp: datetime
    reasoning: str


def compute_entry_exit_schedule(
    earnings_date: date, session: AnnouncementTime
) -> EarningsEntryExitSchedule:
    """The one place this project decides entry/exit timing for a
    forward-tested benchmark decision. See this module's docstring for
    the three cases; ``session`` values other than AFTER_MARKET/
    BEFORE_MARKET (i.e. UNKNOWN, or any future value) always take the
    conservative BMO-shaped branch -- never assume AMC."""
    if session == AnnouncementTime.AFTER_MARKET:
        decision_date = _nearest_trading_day_on_or_before(earnings_date)
        exit_date = next_trading_day(decision_date)
        reasoning = (
            f"{earnings_date.isoformat()} is an after-market-close (AMC) report -- the release "
            f"happens after the close, so a decision generated and entered at 15:55 ET on "
            f"{decision_date.isoformat()} is still real pre-release data. Exit is "
            f"{exit_date.isoformat()} 15:55 ET, the next full trading session after the release."
        )
    else:
        decision_date = previous_trading_day(earnings_date)
        exit_date = _nearest_trading_day_on_or_after(earnings_date)
        if session == AnnouncementTime.BEFORE_MARKET:
            reasoning = (
                f"{earnings_date.isoformat()} is a before-market-open (BMO) report -- the "
                f"result is already known before that day's open, so entering on "
                f"{earnings_date.isoformat()} itself would be look-ahead bias. Decision "
                f"generation and entry use the previous real trading day, "
                f"{decision_date.isoformat()}, 15:55 ET instead. Exit is "
                f"{exit_date.isoformat()} 15:55 ET, after the market has had a full session to "
                f"react."
            )
        else:
            reasoning = (
                f"{earnings_date.isoformat()}'s announcement time is not known to be AMC -- "
                f"the conservative rule applies (never assume AMC): decision generation and "
                f"entry use the previous real trading day, {decision_date.isoformat()}, 15:55 "
                f"ET. Exit is {exit_date.isoformat()} 15:55 ET."
            )

    return EarningsEntryExitSchedule(
        earnings_date=earnings_date,
        session=session,
        decision_generation_date=decision_date,
        entry_timestamp=datetime.combine(decision_date, ENTRY_EXIT_TIME, tzinfo=EASTERN),
        exit_date=exit_date,
        exit_timestamp=datetime.combine(exit_date, ENTRY_EXIT_TIME, tzinfo=EASTERN),
        reasoning=reasoning,
    )
