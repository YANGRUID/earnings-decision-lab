"""Read-only V4.2 challenger diagnostics.

Registered only outside production (alongside the other V4 experimental
surfaces), because a challenger is research and must not read as part of the
official product. Every route here is a GET: this router cannot write
challenger evidence, and evaluating an event is done deliberately by a
caller, never by fetching a page.
"""

from fastapi import APIRouter

from api.deps import DbSession, TwsProviderDep
from api.exceptions import InvalidRequestError, NotFoundError
from models.v4_shadow import V4ShadowDecision
from services.v4_2_comparison import compare_all_events, compare_event

router = APIRouter(prefix="/v4-2/challenger", tags=["v4-2-challenger"])

COMPARISON_NOTICE = (
    "V4.1 CONTROL vs V4.2 CHALLENGER -- methodology comparison, not a verdict. "
    "V4.2 is not production and has placed nothing. Neither side is described as "
    "better: before forward outcomes exist there is nothing to be better at."
)


def _side(side) -> dict:
    return {
        "methodology": side.methodology,
        "status": side.status,
        "selected_candidate_id": side.selected_candidate_id,
        "strategy": side.strategy,
        "expiration": side.expiration,
        "median_return": side.median_return,
        "worst_return": side.worst_return,
        "positive_scenario_fraction": side.positive_scenario_fraction,
        "no_action_reason": side.no_action_reason,
        "candidates_evaluated": side.candidates_evaluated,
        "candidates_accepted": side.candidates_accepted,
    }


def _comparison(comparison) -> dict:
    return {
        "ticker": comparison.ticker,
        "earnings_calendar_event_id": comparison.earnings_calendar_event_id,
        "observed_at": comparison.observed_at,
        "control": _side(comparison.control),
        "challenger": _side(comparison.challenger),
        "challenger_evidence": comparison.challenger_evidence,
        "configurations": comparison.configurations,
        "differs": comparison.differs,
    }


@router.get("/comparison")
def get_methodology_comparison(db: DbSession) -> dict:
    """Every event with a control decision, and the challenger's answer where
    one has been frozen."""
    comparisons = [_comparison(c) for c in compare_all_events(db)]
    return {
        "notice": COMPARISON_NOTICE,
        "events": comparisons,
        "counts": {
            "events": len(comparisons),
            "challenger_evaluated": sum(
                1 for c in comparisons if c["challenger"]["status"] is not None
            ),
            "differs": sum(1 for c in comparisons if c["differs"]),
        },
    }


@router.get("/comparison/{decision_id}")
def get_event_comparison(db: DbSession, decision_id: int) -> dict:
    decision = db.get(V4ShadowDecision, decision_id)
    if decision is None:
        raise NotFoundError(f"no V4 decision {decision_id}")
    return {"notice": COMPARISON_NOTICE, **_comparison(compare_event(db, decision))}


@router.get("/evidence-readiness")
def get_evidence_readiness(db: DbSession) -> dict:
    """What a parallel run would have to work with, per event -- the honest
    precondition check before anything is activated."""
    comparisons = compare_all_events(db)
    return {
        "notice": COMPARISON_NOTICE,
        "events": [
            {
                "ticker": c.ticker,
                "control": "READY" if c.control.status else "MISSING",
                "challenger": c.challenger_evidence,
            }
            for c in comparisons
        ],
    }


@router.get("/dry-run")
def challenger_dry_run(
    db: DbSession, tws_provider: TwsProviderDep, symbols: str, seconds_budget: float = 60.0
) -> dict:
    """ZERO-WRITE parallel dry run over the shared, lifespan-owned provider.

    Exercises exactly what a parallel run would do for the challenger's own
    new evidence -- complete listed metadata plus the bounded expiry ladder --
    and measures the request budget it costs. Nothing is written: the chain
    capture runs with dry_run=True and no challenger decision is frozen.

    Metadata only. No contract is resolved and no market-data subscription is
    opened, so this is safe to run outside market hours and cannot become a
    chain sweep.
    """
    import time as _time  # noqa: PLC0415
    from datetime import UTC, datetime, timedelta  # noqa: PLC0415

    from models.earnings_calendar_event import EarningsCalendarEvent  # noqa: PLC0415
    from services.v4_2_chain_metadata import capture_chain_metadata  # noqa: PLC0415

    if tws_provider is None:
        raise InvalidRequestError("no shared TWS provider on this process")

    started = _time.monotonic()
    now = datetime.now(UTC)
    out: dict = {
        "notice": COMPARISON_NOTICE,
        "mode": "ZERO_WRITE_DRY_RUN",
        "started_at": now.isoformat(),
        "events": [],
        "totals": {
            "metadata_requests": 0,
            "market_data_requests": 0,
            "contract_detail_requests": 0,
        },
    }

    for raw in symbols.split(","):
        ticker = raw.strip().upper()
        if not ticker:
            continue
        if _time.monotonic() - started > seconds_budget:
            out.setdefault("truncated", []).append(ticker)
            continue
        event = (
            db.query(EarningsCalendarEvent)
            .filter(EarningsCalendarEvent.symbol == ticker)
            .order_by(EarningsCalendarEvent.earnings_date)
            .first()
        )
        entry: dict = {"ticker": ticker}
        leg_started = _time.monotonic()
        try:
            capture = capture_chain_metadata(
                db,
                provider=tws_provider,
                ticker=ticker,
                earnings_calendar_event_id=event.id if event else 0,
                earnings_date=event.earnings_date if event else now.date(),
                settlement_date=(
                    (event.earnings_date + timedelta(days=1)) if event else now.date()
                ),
                decision_date=now.date(),
                observed_at=now,
                dry_run=True,  # never writes
            )
            entry.update(
                {
                    "earnings_date": event.earnings_date.isoformat() if event else None,
                    "listed_expirations": len(capture.expirations or []),
                    "listed_strikes": capture.strike_count,
                    "considered_expiries": capture.considered,
                    "metadata_requests": capture.metadata_requests,
                    "market_data_requests": capture.market_data_requests,
                    "reason": capture.reason,
                }
            )
            out["totals"]["metadata_requests"] += capture.metadata_requests
            out["totals"]["market_data_requests"] += capture.market_data_requests
        except Exception as exc:  # noqa: BLE001 -- a dry run must never raise at this boundary
            entry["error"] = f"{type(exc).__name__}: {exc}"
        entry["latency_ms"] = round((_time.monotonic() - leg_started) * 1000, 1)
        out["events"].append(entry)

    out["total_latency_ms"] = round((_time.monotonic() - started) * 1000, 1)
    out["writes_performed"] = 0
    return out
