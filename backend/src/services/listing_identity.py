"""One earnings report, several listed share classes.

Live evidence (2026-09-16): the calendar held LEN (Finnhub) and LEN.B
(EarningsAPI) for the same Lennar report on the same date. They are two share
classes of one issuer -- SEC CIK 0000920760 for both -- releasing ONE set of
results, and Finnhub's own calendar answers a query for "LEN.B" with the LEN
row. Letting both participate would count one report as two independent
forward observations of the same event. LEN.B also has no listed options
(Nasdaq's option chain page has none for LEN/B), so it could never have been
decided anyway -- it would only ever have shown up as "research not ready".

The rule here is deliberately narrow and needs no network: a share-class
listing (``ROOT.X``) is a duplicate of the SAME DAY's event for the plain root
symbol (``ROOT``) when that event exists. Two suffixed classes with no plain
listing (BF.A / BF.B) are left alone: which one is tradable is an options fact
the eligibility check already establishes per symbol, and guessing a
preference between them would be a hard-coded opinion about each issuer.
"""

import re

from sqlalchemy.orm import Session

from models.earnings_calendar_event import EarningsCalendarEvent

# The calendars write a share class as a single trailing letter after a dot or
# hyphen (BF.B, LEN.B, BRK-B). SEC writes the hyphen form.
_SHARE_CLASS = re.compile(r"^(?P<root>[A-Z]{1,10})[.\-](?P<cls>[A-Z])$")


def share_class_root(symbol: str) -> str | None:
    """``LEN.B`` -> ``LEN``; ``BRK-B`` -> ``BRK``; a plain symbol -> None."""
    match = _SHARE_CLASS.match(symbol.strip().upper())
    return match.group("root") if match else None


def duplicate_listing_of(db: Session, event: EarningsCalendarEvent) -> EarningsCalendarEvent | None:
    """The event this one duplicates, or None when it stands on its own."""
    root = share_class_root(event.symbol)
    if root is None:
        return None
    return (
        db.query(EarningsCalendarEvent)
        .filter(
            EarningsCalendarEvent.symbol == root,
            EarningsCalendarEvent.earnings_date == event.earnings_date,
        )
        .one_or_none()
    )


def duplicate_listing_reason(canonical: EarningsCalendarEvent) -> str:
    return (
        f"duplicate share-class listing of {canonical.symbol}'s "
        f"{canonical.earnings_date.isoformat()} report -- one report is one observation"
    )
