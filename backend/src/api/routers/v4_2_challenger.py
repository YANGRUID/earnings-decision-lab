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

TRACK_RECORD_NOTICE = (
    "V4.2 CHALLENGER -- PARALLEL SHADOW, EXPERIMENTAL FORWARD EVIDENCE. A separate "
    "cohort from the V4.1 Track Record, never merged into it. V4.1 remains the "
    "CONTROL and the official methodology; nothing here has placed an order, and "
    "no claim is made that either methodology is better than the other."
)

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
        "move_edge_status": side.move_edge_status,
        "move_edge_ratio": side.move_edge_ratio,
        "expiry_ladder_position": side.expiry_ladder_position,
        "entry_dte": side.entry_dte,
        "dte_at_settlement": side.dte_at_settlement,
        "lifecycle": side.lifecycle,
    }


def _comparison(comparison) -> dict:
    return {
        "ticker": comparison.ticker,
        "earnings_calendar_event_id": comparison.earnings_calendar_event_id,
        "observed_at": comparison.observed_at,
        "control": _side(comparison.control),
        "challenger": _side(comparison.challenger),
        "challenger_evidence": comparison.challenger_evidence,
        "multi_expiry": comparison.multi_expiry,
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


@router.get("/track-record")
def get_challenger_track_record(db: DbSession) -> dict:
    """The CHALLENGER's own forward record -- a separate cohort.

    Never merged with the V4.1 Track Record and never comparable to it by
    simple subtraction: the primary unit is the EVENT, and the configuration
    statistics below it are six sizings of the same forecast rather than six
    independent observations.
    """
    from services.v4_2_track_record import build_challenger_track_record  # noqa: PLC0415

    record = build_challenger_track_record(db)
    return {
        "notice": TRACK_RECORD_NOTICE,
        "methodology": record.methodology,
        "cohort": record.cohort,
        "events": {
            "observed": record.actions.events_observed,
            "action": record.actions.action_events,
            "no_action": record.actions.no_action_events,
            "failed": record.actions.failed_events,
            "action_rate": record.actions.action_rate,
            "no_action_reasons": record.actions.no_action_reasons,
        },
        "lifecycle": {
            "entries_observed": record.entries_observed,
            "entries_failed": record.entries_failed,
            "settlements_due": record.settlements_due,
            "settled": record.settled,
            "settlement_failed": record.settlement_failed,
        },
        "all_outcomes": _outcomes(record.outcomes),
        "executable_only_outcomes": _outcomes(record.executable_outcomes),
        "settlement_quality": record.settlement_quality,
        "by_configuration": record.by_configuration,
        "by_strategy": record.by_strategy,
        "by_expiry_ladder_position": record.by_expiry_ladder_position,
        "warnings": record.warnings,
    }


@router.get("/operations")
def get_challenger_operations(db: DbSession) -> dict:
    """V4.2's own operational state, kept out of V4.1's counters.

    A challenger failure must never make production readiness red, and
    NO_ACTION is reported as a successful methodology outcome rather than
    filed under failures.
    """
    from core.config import get_settings  # noqa: PLC0415
    from services.v4_2_parallel import (  # noqa: PLC0415
        CHALLENGER_HEALTH_DEGRADED,
        CHALLENGER_HEALTH_DISABLED,
        CHALLENGER_HEALTH_READY,
    )
    from services.v4_2_track_record import build_challenger_track_record  # noqa: PLC0415

    settings = get_settings()
    enabled = bool(settings.v4_2_parallel_enabled)
    record = build_challenger_track_record(db)
    failures = record.entries_failed + record.settlement_failed + record.actions.failed_events
    if not enabled:
        state = CHALLENGER_HEALTH_DISABLED
    elif failures:
        state = CHALLENGER_HEALTH_DEGRADED
    else:
        state = CHALLENGER_HEALTH_READY
    return {
        "notice": TRACK_RECORD_NOTICE,
        "scheduler": {
            "parallel_enabled": enabled,
            "state": state,
            "phase": "challenger",
            "runs_inside": "v4_forward_window",
            "separate_job_registered": False,
            "control_priority": True,
        },
        "counts": {
            "events_evaluated": record.actions.events_observed,
            "action": record.actions.action_events,
            "no_action": record.actions.no_action_events,
            "entry_observed": record.entries_observed,
            "entry_failed": record.entries_failed,
            "settlement_due": record.settlements_due,
            "settled": record.settled,
            "settlement_failed": record.settlement_failed,
            "evaluation_failed": record.actions.failed_events,
        },
        "no_action_is_a_failure": False,
        "affects_v4_1_readiness": False,
    }


@router.get("/multi-expiry-dry-run")
def multi_expiry_dry_run(
    db: DbSession,
    tws_provider: TwsProviderDep,
    symbols: str,
    seconds_budget: float = 120.0,
    max_variants: int = 3,
) -> dict:
    """ZERO-WRITE full-path dry run: metadata, ladder, per-expiry candidate
    construction, exact contract resolution and real quotes.

    Unlike ``/dry-run`` above this DOES open market-data subscriptions, for
    the exact candidate legs only -- which is the whole point: the request
    budget and the latency of the real parallel path cannot be measured
    without paying it once. It remains bounded to at most ``max_variants``
    expiries, never sweeps a chain, and writes nothing at all: no decision,
    no candidate, no entry, no settlement, no chain snapshot.

    Outside market hours the quotes will be delayed or absent. That is
    reported honestly rather than smoothed over, and a run outside market
    hours does NOT satisfy the market-hours dry-run gate.
    """
    import time as _time  # noqa: PLC0415
    from datetime import UTC, datetime, timedelta  # noqa: PLC0415

    from models.earnings_calendar_event import EarningsCalendarEvent  # noqa: PLC0415
    from services.v4_2_multi_expiry import (  # noqa: PLC0415
        build_multi_expiry_universe,
        summarize_multi_expiry,
    )

    if tws_provider is None:
        raise InvalidRequestError("no shared TWS provider on this process")

    started = _time.monotonic()
    now = datetime.now(UTC)
    before = _write_counts(db)
    out: dict = {
        "notice": TRACK_RECORD_NOTICE,
        "mode": "ZERO_WRITE_DRY_RUN",
        "market_state": _market_state(now),
        "started_at": now.isoformat(),
        "max_variants": max_variants,
        "events": [],
    }
    totals = {
        "underlying_quotes": 0,
        "metadata_calls": 0,
        "chain_discovery_calls": 0,
        "selected_leg_quote_calls": 0,
        "unique_contracts_quoted": 0,
        "contracts_deduplicated": 0,
        "total_requests": 0,
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
        earnings_date = event.earnings_date if event else now.date()
        try:
            result = build_multi_expiry_universe(
                provider=tws_provider,
                ticker=ticker,
                as_of=now,
                direction="neutral",
                volatility_view=None,
                earnings_date=earnings_date,
                settlement_date=earnings_date + timedelta(days=1),
                max_variants=max_variants,
            )
            entry = summarize_multi_expiry(result)
            entry["ticker"] = ticker
            entry["earnings_date"] = earnings_date.isoformat()
            entry["market_data_quality"] = result.market_data_quality
            entry["quote_coherence"] = _coherence(result)
            for key in totals:
                if key == "total_requests":
                    totals[key] += result.budget.total
                else:
                    totals[key] += getattr(result.budget, key, 0)
        except Exception as exc:  # noqa: BLE001 -- a dry run must never raise here
            entry = {"ticker": ticker, "error": f"{type(exc).__name__}: {exc}"}
        out["events"].append(entry)

    after = _write_counts(db)
    out["totals"] = totals
    out["total_latency_ms"] = round((_time.monotonic() - started) * 1000, 1)
    out["writes_performed"] = {k: after[k] - before[k] for k in before}
    out["zero_write_verified"] = all(v == 0 for v in out["writes_performed"].values())
    return out


def _outcomes(stats) -> dict:
    return {
        "settled": stats.settled,
        "wins": stats.wins,
        "losses": stats.losses,
        "flat": stats.flat,
        "win_rate": stats.win_rate,
        "median_standardized_return": (
            None
            if stats.median_standardized_return is None
            else str(stats.median_standardized_return)
        ),
        "median_capital_used_return": (
            None
            if stats.median_capital_used_return is None
            else str(stats.median_capital_used_return)
        ),
        "total_realized_pnl": (
            None if stats.total_realized_pnl is None else str(stats.total_realized_pnl)
        ),
    }


def _coherence(result) -> dict:
    """Earliest/latest quote and the worst cross-leg skew actually observed.

    No new rejection threshold is invented here: the released coherence policy
    is what governs a real decision. This reports what was seen.
    """
    stamps = [
        leg.entry_bid_observed_at
        for candidate in result.candidates
        for leg in candidate.context.legs
        if getattr(leg, "entry_bid_observed_at", None) is not None
    ]
    retrieved = [
        stamp
        for candidate in result.candidates
        for stamp in (candidate.leg_retrieved_at or {}).values()
        if stamp is not None
    ]
    stamps = stamps or retrieved
    if not stamps:
        return {"earliest": None, "latest": None, "max_skew_seconds": None}
    return {
        "earliest": min(stamps).isoformat(),
        "latest": max(stamps).isoformat(),
        "max_skew_seconds": (max(stamps) - min(stamps)).total_seconds(),
    }


def _write_counts(db) -> dict[str, int]:
    """Row counts across every table a parallel run could possibly write.

    Measured before and after, so 'zero writes' is a verified fact rather than
    a claim about what the code is supposed to do.
    """
    from models.v4_2_challenger import (  # noqa: PLC0415
        V4ChainMetadataSnapshot,
        V42ChallengerCandidate,
        V42ChallengerCandidateObservation,
        V42ChallengerConfigEntry,
        V42ChallengerConfigResult,
        V42ChallengerConfigSettlement,
        V42ChallengerDecision,
    )
    from models.v4_shadow import V4ShadowCandidate, V4ShadowDecision  # noqa: PLC0415

    return {
        "challenger_decisions": db.query(V42ChallengerDecision).count(),
        "challenger_candidates": db.query(V42ChallengerCandidate).count(),
        "challenger_configs": db.query(V42ChallengerConfigResult).count(),
        "challenger_observations": db.query(V42ChallengerCandidateObservation).count(),
        "challenger_entries": db.query(V42ChallengerConfigEntry).count(),
        "challenger_settlements": db.query(V42ChallengerConfigSettlement).count(),
        "chain_snapshots": db.query(V4ChainMetadataSnapshot).count(),
        "control_decisions": db.query(V4ShadowDecision).count(),
        "control_candidates": db.query(V4ShadowCandidate).count(),
    }


def _market_state(now) -> str:
    """Whether US options are open right now, stated plainly.

    The market-hours gate is not satisfied by a run that merely happened to
    execute; it requires the market to have been open, and a dry run says so
    itself rather than leaving the reader to infer it from a timestamp.
    """
    from analytics.earnings_timing import EASTERN  # noqa: PLC0415

    local = now.astimezone(EASTERN)
    if local.weekday() >= 5:
        return "CLOSED_WEEKEND"
    minutes = local.hour * 60 + local.minute
    if 9 * 60 + 30 <= minutes < 16 * 60:
        return "OPEN_REGULAR_HOURS"
    return "CLOSED_OUTSIDE_REGULAR_HOURS"
