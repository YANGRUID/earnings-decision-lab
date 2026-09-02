"""Legal observation windows for the V4 forward test (V4-only reset,
2026-09-02).

Until now these constants lived in V3 modules (services/decision_pipeline.py,
services/benchmark_exit_capture.py, services/scheduler.py) and the V4
scheduler imported them from there. They are generic timing rules, not V3
methodology, so they live here -- a neutral analytics module with no
pipeline imports -- and the V3 modules are free to be deleted.

* ``LATE_CUTOFF_GRACE``: how long after the legal observation time an
  observation may still start. Beyond it the window is missed; nothing is
  observed late and quietly relabelled.
* ``EARLY_CAPTURE_TOLERANCE``: how early before the legal settlement time
  a settlement may be taken (a scheduler that fires a few seconds early
  must not be treated as "not due").
* ``DECISION_CANDIDATE_LOOKAHEAD_DAYS``: the day-level horizon a decision
  run pre-filters calendar events with, before the exact window test.
* ``DECISION_DEADLINE_ET``: the latest time of day at which a decision
  run may START one more full event evaluation (deadline guard).
"""

from __future__ import annotations

from datetime import datetime, time, timedelta

from analytics.market_session import EASTERN
from models.enums import AnnouncementTime, EarningsTiming

LATE_CUTOFF_GRACE = timedelta(minutes=5)
EARLY_CAPTURE_TOLERANCE = timedelta(minutes=5)
DECISION_CANDIDATE_LOOKAHEAD_DAYS = 10

#: Measured 2026-09-02: one full V4 evaluation (DeepSeek V4 Pro thinking
#: view ~85 s + assembly, one quote sweep, six configurations ~45 s) takes
#: about 130 s; the 15:30 run must not start an evaluation that could still
#: be quoting after the close. 15:50 ET leaves ~10 minutes for the last
#: started event.
DECISION_DEADLINE_ET = time(15, 50)

#: Calendar timing code -> announcement session. DMH ("during market
#: hours") and UNKNOWN take the conservative BMO-shaped schedule (decide
#: the trading day before, settle on the earnings day), never AMC.
TIMING_TO_ANNOUNCEMENT_TIME: dict[EarningsTiming, AnnouncementTime] = {
    timing: (
        AnnouncementTime.AFTER_MARKET
        if timing is EarningsTiming.AMC
        else AnnouncementTime.BEFORE_MARKET
        if timing is EarningsTiming.BMO
        else AnnouncementTime.UNKNOWN
    )
    for timing in EarningsTiming
}


def coerce_earnings_timing(value) -> EarningsTiming:
    """An ORM row can carry the enum or, freshly flushed, its raw name."""
    if isinstance(value, EarningsTiming):
        return value
    try:
        return EarningsTiming(value)
    except ValueError:
        return EarningsTiming[str(value).upper()]


def announcement_session(event) -> AnnouncementTime:
    return TIMING_TO_ANNOUNCEMENT_TIME[coerce_earnings_timing(event.earnings_time)]


def decision_deadline_for(now: datetime) -> datetime:
    """The deadline instant on ``now``'s Eastern calendar day."""
    local = now.astimezone(EASTERN)
    return datetime.combine(local.date(), DECISION_DEADLINE_ET, tzinfo=EASTERN)
