"""Point-in-time access to a company's historical post-earnings moves.

The one invariant this module exists to enforce: a distribution built for a
decision at time T contains only earnings events that had already REPORTED
strictly before T. Nothing else in the codebase enforces that --
``services/historical_moves.py`` filters by company and optionally excludes a
single event id, which is right for a page rendering one event but would let
a later earnings report leak into an earlier decision's evidence.

Audited 2026-09-05: the V4.1 production path never passes historical moves to
``derive_expected_move_context`` at all, so every one of the seven natural
events recorded ``historical_sample_n = 0`` while 1,201 real reactions across
50 companies sat in the database. That wiring gap is reported separately and
is deliberately NOT fixed here -- the strike engine consults
``historical_median_abs_move_pct``, so wiring it would change V4.1's strike
geometry, which this task must not do.
"""

from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from analytics.earnings.v4_2_move_distribution import (
    TIMING_CLOSE_TO_CLOSE,
    TIMING_UNVERIFIED,
    TIMING_VERIFIED,
    MoveDistribution,
    build_move_distribution,
)
from analytics.earnings.v4_2_reaction_anchoring import (
    AnchoredReaction,
    aggregate_timing_quality,
    anchored_reaction,
    classify_announcement_time,
)
from models.company import Company
from models.earnings_event import EarningsEvent
from models.price_bar import PriceBar
from models.price_reaction import PriceReaction


def historical_moves_before(
    db: Session, company_id: int, before: date
) -> tuple[list[Decimal], bool]:
    """Every real ``next_day_move_pct`` for ``company_id`` whose earnings
    event reported STRICTLY before ``before``.

    Returns the moves and whether the announcement timing behind them was
    known for every contributing event -- the caller needs that to label the
    distribution's provenance honestly rather than assuming AMC anchoring.
    """
    rows = (
        db.query(PriceReaction.next_day_move_pct, EarningsEvent.announcement_time)
        .join(EarningsEvent, PriceReaction.earnings_event_id == EarningsEvent.id)
        .filter(
            EarningsEvent.company_id == company_id,
            PriceReaction.next_day_move_pct.isnot(None),
            EarningsEvent.earnings_date.isnot(None),
            # Strictly before: an event reporting ON the decision date has not
            # yet produced its post-earnings observation, and the event being
            # decided must never appear in its own baseline.
            EarningsEvent.earnings_date < before,
        )
        .all()
    )
    moves = [r[0] for r in rows]
    timing_known = bool(rows) and all(
        getattr(r[1], "value", str(r[1])) != "UNKNOWN" for r in rows
    )
    return moves, timing_known


def move_distribution_for(
    db: Session, *, company_id: int, as_of: date
) -> MoveDistribution:
    """The point-in-time distribution a V4.2 decision at ``as_of`` may use."""
    moves, timing_known = historical_moves_before(db, company_id, as_of)
    return build_move_distribution(
        moves,
        as_of=as_of,
        timing_method=TIMING_CLOSE_TO_CLOSE,
        timing_provenance=TIMING_VERIFIED if timing_known else TIMING_UNVERIFIED,
    )


def move_distribution_for_ticker(
    db: Session, *, ticker: str, as_of: date
) -> MoveDistribution:
    """Convenience for the replay and diagnostics paths, which work from the
    decision's ticker rather than a company id."""
    company = db.query(Company).filter(Company.ticker == ticker).one_or_none()
    if company is None:
        return build_move_distribution(
            [], as_of=as_of, timing_method=TIMING_CLOSE_TO_CLOSE,
            timing_provenance=TIMING_UNVERIFIED,
        )
    return move_distribution_for(db, company_id=company.id, as_of=as_of)


def anchored_moves_before(
    db: Session, company_id: int, before: date
) -> tuple[list[AnchoredReaction], list[int]]:
    """Point-in-time anchored reactions, recomputed from immutable price bars.

    Unlike ``historical_moves_before`` -- which reads the existing
    ``PriceReaction`` corpus and therefore inherits its AMC-only anchoring --
    this re-derives each move under the versioned AMC/BMO rule. It writes
    nothing and leaves the persisted corpus exactly as it is.

    Point-in-time on BOTH sides: only events that reported strictly before
    ``before``, and only bars dated strictly before it, so a post-earnings
    observation that had not yet happened cannot contribute.
    """
    events = (
        db.query(EarningsEvent)
        .filter(
            EarningsEvent.company_id == company_id,
            EarningsEvent.earnings_date.isnot(None),
            EarningsEvent.earnings_date < before,
        )
        .all()
    )
    if not events:
        return [], []

    company = db.get(Company, company_id)
    if company is None:
        return [], []

    bars: dict[date, Decimal] = {
        row.trade_date: row.close
        for row in db.query(PriceBar).filter(
            PriceBar.ticker == company.ticker,
            # A bar dated on or after the decision boundary is future
            # information and must not be able to anchor anything.
            PriceBar.trade_date < before,
        )
    }

    reactions: list[AnchoredReaction] = []
    contributing: list[int] = []
    for event in events:
        if event.earnings_date is None:  # defensive: the query already excludes these
            continue
        reaction = anchored_reaction(
            bars,
            earnings_date=event.earnings_date,
            timing_classification=classify_announcement_time(event.announcement_time),
        )
        if reaction is None:
            continue
        reactions.append(reaction)
        contributing.append(event.id)
    return reactions, contributing


def anchored_move_distribution_for(
    db: Session, *, company_id: int, as_of: date
) -> MoveDistribution:
    """The V4.2 distribution: versioned anchoring, point-in-time safe, with
    its timing quality and a digest of the events behind it."""
    reactions, event_ids = anchored_moves_before(db, company_id, as_of)
    return build_move_distribution(
        [r.signed_move_pct for r in reactions],
        as_of=as_of,
        timing_method=TIMING_CLOSE_TO_CLOSE,
        timing_provenance=(
            TIMING_VERIFIED
            if reactions and all(r.timing_verified for r in reactions)
            else TIMING_UNVERIFIED
        ),
        timing_quality=aggregate_timing_quality(reactions),
        source_event_ids=event_ids,
    )


def anchored_move_distribution_for_ticker(
    db: Session, *, ticker: str, as_of: date
) -> MoveDistribution:
    company = db.query(Company).filter(Company.ticker == ticker).one_or_none()
    if company is None:
        return build_move_distribution([], as_of=as_of)
    return anchored_move_distribution_for(db, company_id=company.id, as_of=as_of)
