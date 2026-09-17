"""Operator verification of an earnings date, with its source.

Why this exists (2026-09-17 audit). With EarningsAPI's allowance spent, the
calendar ran on Finnhub alone, and Finnhub was checked against issuers' own
announcements for the eligible events of the following two weeks:

* GIS was listed on 2026-09-15; General Mills reports on 2026-09-23 before the
  open;
* FERG was listed on 2026-09-22; Ferguson now reports calendar quarters and
  reported its second quarter on 2026-08-10, so there is no September report;
* ACN was listed as after the close on 2026-10-01; Accenture's call is at
  08:00 ET that morning. Deciding it at 15:30 that day would be after the
  release -- look-ahead, not a forward observation.

A provider sync never changes a verified row (services/earnings_calendar_sync.
py); a disagreement is recorded as a conflict. Verification changes calendar
rows only: it refuses to move an event that already carries a frozen V4
decision, because that would rewrite what the decision refers to.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from models.earnings_calendar_event import EarningsCalendarEvent
from models.enums import EarningsCalendarEventStatus, EarningsTiming


class CalendarVerificationError(ValueError):
    pass


@dataclass(frozen=True)
class CalendarVerification:
    event_id: int
    symbol: str
    before: dict[str, str | None]
    after: dict[str, str | None]


def _snapshot(row: EarningsCalendarEvent) -> dict[str, str | None]:
    return {
        "earnings_date": row.earnings_date.isoformat(),
        "earnings_time": getattr(row.earnings_time, "value", str(row.earnings_time)),
        "status": getattr(row.status, "value", str(row.status)),
        "verified_at": row.verified_at.isoformat() if row.verified_at else None,
        "verification_note": row.verification_note,
    }


def apply_calendar_verification(
    db: Session,
    event: EarningsCalendarEvent,
    *,
    note: str,
    earnings_date: date | None = None,
    timing: EarningsTiming | None = None,
    no_report: bool = False,
    now: datetime | None = None,
) -> CalendarVerification:
    """Records what the issuer itself announced for this event.

    ``no_report=True`` verifies that no report takes place on the stored date
    (the row becomes SKIPPED and can never be decided). Otherwise the event is
    live (UPCOMING) on ``earnings_date`` (default: unchanged) with ``timing``
    (default: unchanged). ``note`` must name the source.
    """
    if not note.strip():
        raise CalendarVerificationError("a verification must name its source")
    if no_report and (earnings_date is not None or timing is not None):
        raise CalendarVerificationError("a verified non-event carries no date or timing")

    from models.v4_shadow import V4ShadowDecision  # noqa: PLC0415 -- avoids a model import cycle

    has_decision = (
        db.query(V4ShadowDecision.id).filter_by(earnings_calendar_event_id=event.id).first()
        is not None
    )
    moving = earnings_date is not None and earnings_date != event.earnings_date
    if has_decision and (moving or no_report or timing is not None):
        raise CalendarVerificationError(
            f"{event.symbol} on {event.earnings_date.isoformat()} carries a frozen V4 decision; "
            "its date, timing and status are evidence and are not changed by verification"
        )
    if earnings_date is not None and moving:
        clash = (
            db.query(EarningsCalendarEvent)
            .filter(
                EarningsCalendarEvent.symbol == event.symbol,
                EarningsCalendarEvent.earnings_date == earnings_date,
                EarningsCalendarEvent.id != event.id,
            )
            .one_or_none()
        )
        if clash is not None:
            raise CalendarVerificationError(
                f"{event.symbol} already has an event on {earnings_date.isoformat()} "
                f"(id {clash.id}); verify that row instead"
            )

    before = _snapshot(event)
    now = now or datetime.now(UTC)
    if no_report:
        event.status = EarningsCalendarEventStatus.SKIPPED
    else:
        if earnings_date is not None:
            event.earnings_date = earnings_date
        if timing is not None:
            event.earnings_time = timing
        event.status = EarningsCalendarEventStatus.UPCOMING
        event.vanished_by = None
        event.vanished_at = None
    event.verified_at = now
    event.verification_note = note.strip()
    db.flush()
    return CalendarVerification(event.id, event.symbol, before, _snapshot(event))


def create_verified_event(
    db: Session,
    *,
    symbol: str,
    earnings_date: date,
    timing: EarningsTiming,
    note: str,
    company_name: str,
    market_cap: Decimal | None,
    country: str | None,
    now: datetime | None = None,
) -> CalendarVerification:
    """An issuer-announced report the providers do not list on its real date.

    Live (2026-09-17): Carnival reports on 2026-09-29 and Nike on 2026-10-01;
    Finnhub listed both on 2026-09-28 and EarningsAPI could not be asked.
    Without a verified row the placeholder would have been created that night
    and decided a trading day early -- before a report that had not happened.
    With one, the placeholder is recorded as a conflict and never becomes an
    event. The profile fields come from a provider profile lookup."""
    if not note.strip():
        raise CalendarVerificationError("a verification must name its source")
    symbol = symbol.strip().upper()
    clash = (
        db.query(EarningsCalendarEvent)
        .filter(
            EarningsCalendarEvent.symbol == symbol,
            EarningsCalendarEvent.earnings_date == earnings_date,
        )
        .one_or_none()
    )
    if clash is not None:
        raise CalendarVerificationError(
            f"{symbol} already has an event on {earnings_date.isoformat()} (id {clash.id}); "
            "verify that row instead"
        )
    now = now or datetime.now(UTC)
    row = EarningsCalendarEvent(
        symbol=symbol,
        company_name=company_name,
        earnings_date=earnings_date,
        earnings_time=timing,
        market_cap=market_cap,
        country=country,
        status=EarningsCalendarEventStatus.UPCOMING,
        profile_refreshed_at=now if market_cap is not None else None,
        verified_at=now,
        verification_note=note.strip(),
    )
    db.add(row)
    db.flush()
    return CalendarVerification(row.id, symbol, {}, _snapshot(row))


def main(argv: list[str] | None = None) -> int:
    """``python -m services.calendar_verification --symbol GIS --date 2026-09-15
    --new-date 2026-09-23 --timing BMO --note "<source>"`` (or ``--no-report``).
    Prints the before/after record as JSON; ``--apply`` commits, without it the
    change is rolled back (a dry run)."""
    import argparse  # noqa: PLC0415
    import json  # noqa: PLC0415

    from db.session import SessionLocal  # noqa: PLC0415

    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--date", required=True, type=date.fromisoformat)
    parser.add_argument("--new-date", type=date.fromisoformat)
    parser.add_argument("--timing", choices=[t.name for t in EarningsTiming])
    parser.add_argument("--no-report", action="store_true")
    parser.add_argument("--note", required=True)
    parser.add_argument("--create", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    db = SessionLocal()
    try:
        if args.create:
            if not args.timing:
                print(json.dumps({"error": "--create needs --timing"}))
                return 2
            profile = _provider_profile(db, args.symbol)
            if profile is None or profile.name is None:
                print(json.dumps({"error": f"no provider profile for {args.symbol}"}))
                return 2
            created = create_verified_event(
                db,
                symbol=args.symbol,
                earnings_date=args.date,
                timing=EarningsTiming[args.timing],
                note=args.note,
                company_name=profile.name,
                market_cap=_dollars(profile),
                country=profile.country,
            )
            if args.apply:
                db.commit()
            else:
                db.rollback()
            print(json.dumps({"applied": args.apply, "created": created.after}))
            return 0
        event = (
            db.query(EarningsCalendarEvent)
            .filter_by(symbol=args.symbol.upper(), earnings_date=args.date)
            .one_or_none()
        )
        if event is None:
            print(json.dumps({"error": f"no {args.symbol} event on {args.date.isoformat()}"}))
            return 2
        result = apply_calendar_verification(
            db,
            event,
            note=args.note,
            earnings_date=args.new_date,
            timing=EarningsTiming[args.timing] if args.timing else None,
            no_report=args.no_report,
        )
        if args.apply:
            db.commit()
        else:
            db.rollback()
        print(
            json.dumps(
                {
                    "applied": args.apply,
                    "event_id": result.event_id,
                    "symbol": result.symbol,
                    "before": result.before,
                    "after": result.after,
                }
            )
        )
        return 0
    except CalendarVerificationError as exc:
        db.rollback()
        print(json.dumps({"error": str(exc)}))
        return 3
    finally:
        db.close()


def _provider_profile(db: Session, symbol: str):  # noqa: ANN202 -- FinnhubCompanyProfile | None
    from core.config import get_settings  # noqa: PLC0415
    from providers.factory import build_earnings_calendar_provider  # noqa: PLC0415

    provider = build_earnings_calendar_provider(get_settings(), db)
    return provider.get_company_profile(symbol.upper()) if provider is not None else None


def _dollars(profile) -> Decimal | None:  # noqa: ANN001 -- FinnhubCompanyProfile
    from services.earnings_calendar_sync import _market_cap_dollars  # noqa: PLC0415

    return _market_cap_dollars(profile)


if __name__ == "__main__":
    raise SystemExit(main())
