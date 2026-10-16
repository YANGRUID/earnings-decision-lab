"""Which trading session does a stranded V4 position actually settle in?

Every end-of-day recovery has to answer this before it may price anything,
because the released fallback hierarchy is defined entirely in terms of ONE
session: "that option contract's own final verifiable same-session closing
mark for the settlement date". Price a position from any other session and
the number is not a late settlement, it is a different trade.

The authoritative answer is the one the scheduler itself uses --
``v4_schedule_for_event(event).exit_date``, derived from the calendar event
and the active timing policy. Nothing here re-derives timing.

Why this module exists at all (real incident, 2026-10-16): both recovery
services used to infer the settlement session from the FAILED ATTEMPT's own
``settled_at`` timestamp. That is the moment the failure was *detected*, not
the session the position was due in, and the two diverge exactly when it
matters most:

  * NKE's legal window was 2026-10-02 15:30 ET. The Mac slept over the
    weekend, so the miss was first detected on 2026-10-05 and the failed
    rows carry that stamp. A recovery keyed on the stamp looks for a close
    on 10-05 -- three sessions after the contracts expired -- and finds
    nothing, forever. Keyed on the real session it settles correctly from
    the 10-02 expiration close.
  * the challenger path had no session scoping at all, so the daily 16:30
    job swept up JNJ (due 2026-10-13) and priced it from the 2026-10-16
    close: three extra sessions of drift, recorded as a settlement.

A position whose calendar event has gone is deliberately left unresolved:
without the event there is no legal session, and a recovery that guesses
one is worse than a position that stays open.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy.orm import Session

from analytics.market_session import EASTERN
from models.earnings_calendar_event import EarningsCalendarEvent


def eastern_date(moment: datetime) -> date:
    """Which US trading date a moment belongs to."""
    return moment.astimezone(EASTERN).date()


def session_has_closed(now: datetime, session_date: date) -> bool:
    """True once the regular US session on ``session_date`` has ended.

    A closing mark that does not yet exist must not be used as one; this is
    what keeps MARKET_CLOSE_FALLBACK meaning what the released methodology
    says it means.
    """
    local = now.astimezone(EASTERN)
    if local.date() > session_date:
        return True
    if local.date() < session_date:
        return False
    return (local.hour, local.minute) >= (16, 0)


class SettlementSessionResolver:
    """Decision id -> its legal settlement session, memoized per run.

    One instance per recovery run: a run touches a handful of decisions and
    each one's schedule is pure, so the cache is just there to keep a sweep
    over several sessions from re-reading the same calendar rows.
    """

    def __init__(self, db: Session) -> None:
        self._db = db
        self._by_event: dict[int, date | None] = {}

    def for_event(self, event_id: int | None) -> date | None:
        if event_id is None:
            return None
        if event_id in self._by_event:
            return self._by_event[event_id]
        resolved = self._resolve(event_id)
        self._by_event[event_id] = resolved
        return resolved

    def _resolve(self, event_id: int) -> date | None:
        from services.v4_shadow_scheduler import v4_schedule_for_event  # noqa: PLC0415

        event = self._db.get(EarningsCalendarEvent, event_id)
        if event is None or event.earnings_date is None:
            return None
        try:
            return v4_schedule_for_event(event).exit_date
        except (KeyError, ValueError):
            # An unconfirmed/unknown announcement session has no legal
            # forward window at all; such an event should never have
            # produced a decision, and if one exists it is not settleable.
            return None


__all__ = ["SettlementSessionResolver", "eastern_date", "session_has_closed"]
