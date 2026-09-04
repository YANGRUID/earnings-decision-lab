"""IBKR TWS Migration, production cutover (2026-09-01) -- one READ-ONLY
diagnostic endpoint that exercises the real, lifespan-owned shared
IBKRTWSProvider from INSIDE this FastAPI process.

Why this exists at all, rather than a script: providers/factory.py's
``_shared_tws_provider`` is a module-level global, so it is only ever set
in the process whose lifespan called ``set_shared_tws_provider``. A
separate process -- a standalone script, or ``docker compose exec backend
python -c ...`` -- imports a *fresh* copy of that module where the global
is ``None``, and therefore silently constructs a SECOND connection at the
same ``ibkr_tws_client_id``. Confirmed live during this cutover: such a
process connected at client id 101 (the production provider's own id) and
held it, which would have produced a real IB Gateway error 326 for
production the moment it needed that id. Verifying the production
connection therefore has to happen in-process, which is what this is.

Strictly read-only and strictly diagnostic:
  * no database session is opened, so no row can be written;
  * no credential, account id, username, or session token is read or
    returned (the provider exposes none of those to begin with);
  * order placement/cancellation/exercise are permanently blocked at the
    ibapi client level (providers/ibkr_tws_client.py) and are not
    reachable from here regardless;
  * every call below is one this project's own production code path
    already makes for ordinary market-data collection.
"""

import time
from datetime import UTC, date, datetime
from decimal import Decimal

from fastapi import APIRouter

from analytics.decision.v4_4b_ranking import rank_candidates
from api.deps import DbSession, TwsProviderDep
from api.exceptions import InvalidRequestError
from providers.ibkr_client import IBKRError
from schemas.api import TwsProductionSanityResponse
from services.v4_shadow import evaluate_shadow_candidate
from services.v4_shadow_assembler import assemble_shadow_candidates, summarize_assembly

router = APIRouter(prefix="/internal/ibkr", tags=["tws-diagnostics"])

# A real, always-listed, highly liquid probe symbol -- the same one
# services/provider_test_connection.py already uses, for the same reason.
_PROBE_TICKER = "AAPL"

DRY_RUN_NOTICE = (
    "V4 SHADOW DRY-RUN -- EXPERIMENTAL, READ-ONLY. Nothing is persisted: no shadow "
    "decision, no official V3 record, no entry or settlement attempt. No brokerage order "
    "is placed. Results must NOT be used to retune ranking v1 or the scenario grid."
)


@router.get("/tws-production-sanity", response_model=TwsProductionSanityResponse)
def tws_production_sanity(tws_provider: TwsProviderDep) -> TwsProductionSanityResponse:
    """Read-only proof that the shared production TWS connection can
    actually serve real market data: one underlying quote, then one
    exact option contract resolved and quoted. Never writes anything."""
    if tws_provider is None:
        raise InvalidRequestError(
            "no shared TWS provider on this process -- ibkr_provider is not 'tws'"
        )

    snapshot = tws_provider._connection.health_snapshot()  # noqa: SLF001 -- diagnostic by design
    started = time.monotonic()

    try:
        underlying = tws_provider.get_underlying_quote(_PROBE_TICKER)
    except IBKRError as exc:
        raise InvalidRequestError(f"underlying quote failed: {exc}") from exc
    if underlying is None:
        raise InvalidRequestError(f"no underlying quote returned for {_PROBE_TICKER}")
    underlying_elapsed_ms = (time.monotonic() - started) * 1000

    # Resolve exactly ONE real, near-ATM contract at the next listed
    # expiration -- deliberately not a whole chain sweep (Section 13's
    # own rule: quote only selected contracts).
    option_started = time.monotonic()
    try:
        expiration = tws_provider.resolve_expiration_for_reconstruction(
            _PROBE_TICKER, date.today(), None
        )
        if expiration is None:
            raise InvalidRequestError("no listed expiration resolved")
        _conid, _price, contracts = tws_provider.discover_contracts_for_expiration(
            _PROBE_TICKER, expiration
        )
    except IBKRError as exc:
        raise InvalidRequestError(f"contract resolution failed: {exc}") from exc
    if not contracts:
        raise InvalidRequestError(f"no contracts resolved for {_PROBE_TICKER} {expiration}")

    strike, right, option_conid = min(contracts, key=lambda c: (abs(c[0] - underlying.price), c[1]))
    from providers.types import SelectedLeg  # noqa: PLC0415 -- local, diagnostic-only

    leg = SelectedLeg(strike=strike, option_type="call" if right == "C" else "put", action="buy")
    try:
        quotes = tws_provider.get_quotes_for_selected_legs(
            _PROBE_TICKER, [leg], expiration, datetime.now(UTC)
        )
    except IBKRError as exc:
        raise InvalidRequestError(f"option quote failed: {exc}") from exc
    if not quotes:
        raise InvalidRequestError("no option quote returned")
    option = quotes[0]
    option_elapsed_ms = (time.monotonic() - option_started) * 1000

    return TwsProductionSanityResponse(
        shared_provider_reused=True,
        reconnect_state=snapshot.reconnect_state,
        api_ready=snapshot.api_ready,
        underlying_ticker=underlying.ticker,
        underlying_price=underlying.price,
        underlying_bid=underlying.bid,
        underlying_ask=underlying.ask,
        underlying_quality=underlying.market_data_quality,
        underlying_source_provider=underlying.source_provider,
        underlying_elapsed_ms=underlying_elapsed_ms,
        option_expiration=expiration,
        option_strike=strike,
        option_right=right,
        option_conid=option_conid,
        option_bid=option.bid,
        option_ask=option.ask,
        option_last=option.last_price,
        option_quality=option.market_data_quality,
        option_source_provider=option.source_provider,
        option_elapsed_ms=option_elapsed_ms,
    )


# --------------------------------------------------------------------------
# V4.5 -- V4 shadow DRY-RUN (Sections 40, 41, 70).
#
# Read-only rehearsal of the live shadow pipeline against real market
# data. It exists to measure latency and the TWS request budget before
# activation, and it is deliberately incapable of persisting anything:
#   * no DB session is opened, so no V4ShadowDecision / V4ShadowSettlement
#     / V3 DecisionSnapshot / entry attempt / settlement attempt can be
#     written;
#   * no order path is reachable (the provider blocks every order method
#     at the ibapi level);
#   * it lives behind ENABLE_INTERNAL_DIAGNOSTICS, so it is absent from
#     the normal production API surface.
#
# Section 45: whatever this selects must NOT be used to retune ranking
# v1, the bands, or the scenario grid. It validates infrastructure and
# performance -- nothing about methodology.
# --------------------------------------------------------------------------


@router.get("/v4-shadow-dry-run", response_model=None)
def v4_shadow_dry_run(
    tws_provider: TwsProviderDep,
    ticker: str = "AAPL",
    direction: str = "neutral",
    volatility_view: str | None = None,
) -> dict:
    """Assembles and ranks a live candidate set WITHOUT persisting it."""
    if tws_provider is None:
        raise InvalidRequestError(
            "no shared TWS provider on this process -- ibkr_provider is not 'tws'"
        )

    started = time.monotonic()
    assembly = assemble_shadow_candidates(
        provider=tws_provider,
        ticker=ticker,
        as_of=datetime.now(UTC),
        direction=direction,
        volatility_view=volatility_view,
    )
    if assembly.failure_category is not None:
        return {
            "notice": DRY_RUN_NOTICE,
            "ticker": ticker,
            "status": "FAILED",
            "failure_category": assembly.failure_category,
            "failure_detail": assembly.failure_detail,
            "assembly": summarize_assembly(assembly),
        }

    ranking_started = time.monotonic()
    evaluated = [evaluate_shadow_candidate(c) for c in assembly.candidates]
    ranked = rank_candidates([r for r, _stress in evaluated])
    ranking_ms = (time.monotonic() - ranking_started) * 1000

    top = next((r for r in ranked if r.rank == 1), None)
    return {
        "notice": DRY_RUN_NOTICE,
        "ticker": ticker,
        "status": "RANKED" if top is not None else "NO_ACTION",
        "assembly": summarize_assembly(assembly),
        "valuation_and_ranking_ms": round(ranking_ms, 3),
        "total_dry_run_ms": round((time.monotonic() - started) * 1000, 3),
        "candidate_count": len(ranked),
        "rankable_count": sum(1 for r in ranked if r.rank is not None),
        "validity_breakdown": {
            status: sum(1 for r in ranked if r.status == status)
            for status in sorted({r.status for r in ranked})
        },
        "rank_1": (
            {
                "candidate_id": top.candidate_id,
                "strategy": top.strategy,
                "expiration": top.expiration,
                "semantic_compatibility": str(top.semantic_compatibility)
                if top.semantic_compatibility is not None
                else None,
                "core_worst_return": str(top.worst_executable_return)
                if top.worst_executable_return is not None
                else None,
                "core_median_return": str(top.median_executable_return)
                if top.median_executable_return is not None
                else None,
                "core_positive_scenario_fraction": str(top.positive_scenario_fraction)
                if top.positive_scenario_fraction is not None
                else None,
                "mean_relative_spread": str(top.mean_relative_spread)
                if top.mean_relative_spread is not None
                else None,
                "capital_utilisation": str(top.capital_utilisation)
                if top.capital_utilisation is not None
                else None,
                "market_data_quality": top.market_data_quality,
                "no_profitable_region": top.robustness.no_profitable_region,
                "rationale": top.rationale,
            }
            if top is not None
            else None
        ),
        "persisted": False,
        "orders_placed": 0,
    }


# --------------------------------------------------------------------------
# Activation-phase live dry-run (Sections 34-43): the FULL six-cohort path,
# in-process on the lifespan-owned TWS provider, persisting NOTHING.
#
# Everything generate_shadow_decision does -- decision row, shared
# candidates, six configuration results, candidate-level observations and
# per-configuration entries -- is executed inside an outer SAVEPOINT that is
# rolled back after the telemetry has been read. The DecisionView call is
# real (it is part of the path being validated). No order, no write.
# --------------------------------------------------------------------------
@router.get("/v4-cohort-dry-run", response_model=None)
def v4_cohort_dry_run(
    tws_provider: TwsProviderDep,
    db: DbSession,
    ticker: str = "AAPL",
    direction_fallback: str = "neutral",
) -> dict:
    from decimal import Decimal  # noqa: PLC0415

    from models.company import Company  # noqa: PLC0415
    from models.earnings_calendar_event import EarningsCalendarEvent  # noqa: PLC0415
    from models.v4_shadow import (  # noqa: PLC0415
        V4ShadowCandidateObservation,
        V4ShadowConfigEntry,
        V4ShadowConfigResult,
        V4ShadowDecision,
    )
    from services.v4_shadow import generate_shadow_decision  # noqa: PLC0415
    from services.v4_shadow_orchestration import default_view_generator  # noqa: PLC0415

    if tws_provider is None:
        raise InvalidRequestError(
            "no shared TWS provider on this process -- ibkr_provider is not 'tws'"
        )

    now = datetime.now(UTC)
    timings: dict[str, float] = {}
    t0 = time.monotonic()

    # 1. DecisionView -- real, from prepared research (one call).
    company = db.query(Company).filter_by(ticker=ticker.upper()).one_or_none()
    event = (
        db.query(EarningsCalendarEvent)
        .filter_by(symbol=ticker.upper())
        .order_by(EarningsCalendarEvent.earnings_date.desc())
        .first()
    )
    view = None
    view_note = None
    t = time.monotonic()
    if company is not None and event is not None:
        try:
            view = default_view_generator(db, company, event, now)
        except Exception as exc:  # noqa: BLE001 -- reported, never fatal to the dry-run
            view_note = f"view generation failed: {type(exc).__name__}: {exc}"
    else:
        view_note = "no company/calendar row for ticker -- view generation skipped"
    timings["decision_view_ms"] = (time.monotonic() - t) * 1000
    direction = view.direction if view and view.direction else direction_fallback
    volatility_view = view.volatility_view if view else None

    # 2. Common evidence + candidate universe (one assembly, one quote sweep).
    t = time.monotonic()
    assembly = assemble_shadow_candidates(
        provider=tws_provider,
        ticker=ticker.upper(),
        as_of=now,
        direction=direction,
        volatility_view=volatility_view,
        earnings_date=event.earnings_date if event is not None else None,
    )
    timings["assembly_ms"] = (time.monotonic() - t) * 1000
    if assembly.failure_category is not None:
        return {
            "notice": DRY_RUN_NOTICE,
            "ticker": ticker.upper(),
            "status": "FAILED",
            "failure_category": assembly.failure_category,
            "failure_detail": assembly.failure_detail,
            "assembly": summarize_assembly(assembly),
            "view_note": view_note,
            "persisted": False,
            "orders_placed": 0,
        }

    # 3. Full freeze inside an OUTER savepoint that is always rolled back.
    outer = db.begin_nested()
    result = None
    configs: list = []
    entries: list = []
    observations: list = []
    transient_calendar_event = False
    try:
        if event is not None:
            freeze_event_id = event.id
        else:
            # Section 36: a ticker outside the calendar universe (AAPL is
            # not in an upcoming window) must still exercise the FULL
            # freeze path -- six configurations, candidate observations,
            # per-configuration entries -- which needs a real calendar row
            # for the foreign key. The row lives only inside this
            # savepoint, which is rolled back unconditionally below: it is
            # never committed, never visible to any other request, never
            # picked up by a scheduler. No history is created.
            from analytics.earnings_timing import next_trading_day  # noqa: PLC0415

            placeholder = EarningsCalendarEvent(
                symbol=ticker.upper(),
                company_name=ticker.upper(),
                earnings_date=next_trading_day(now.date()),
                earnings_time="AMC",
                source="EARNINGSAPI",
                status="UPCOMING",
            )
            db.add(placeholder)
            db.flush()
            freeze_event_id = placeholder.id
            transient_calendar_event = True

        t = time.monotonic()
        result = generate_shadow_decision(
            db,
            earnings_calendar_event_id=freeze_event_id,
            ticker=ticker.upper(),
            company_name=company.name if company else ticker.upper(),
            legal_decision_window_at=now,
            as_of=now,
            view=view
            or __import__("services.v4_shadow", fromlist=["ShadowDecisionView"]).ShadowDecisionView(
                direction=direction,
                volatility_view=volatility_view,
                expected_move_intent=None,
                confidence=None,
                reasoning="dry-run fallback view",
                evidence_refs={},
                llm_provider=None,
                llm_model=None,
                prompt_version=None,
            ),
            candidates=assembly.candidates,
            underlying_price=assembly.underlying_price,
            underlying_quote_at=assembly.underlying_quote_at,
            market_data_quality=assembly.market_data_quality,
            tws_request_count=assembly.budget.total,
            unique_contracts_quoted=assembly.budget.unique_contracts_quoted,
        )
        timings["freeze_valuation_ranking_sixconfig_ms"] = (time.monotonic() - t) * 1000
        if result.decision_id:
            configs = [
                {
                    "configuration_key": r.configuration_key,
                    "status": r.status,
                    "rank_1_candidate_id": r.rank_1_candidate_id,
                    "eligible": r.eligible_candidate_count,
                    "excluded": r.excluded_candidate_count,
                    "no_action_reason": r.no_action_reason,
                }
                for r in db.query(V4ShadowConfigResult).filter_by(
                    shadow_decision_id=result.decision_id
                )
            ]
            entries = [
                {
                    "configuration_key": e.configuration_key,
                    "candidate_id": e.candidate_id,
                    "status": e.status,
                    "quantity": e.quantity,
                    "capital_used": str(e.capital_used),
                    "max_risk_used": str(e.max_risk_used),
                    "entry_net_value": str(e.entry_net_value),
                    "candidate_observation_id": e.candidate_observation_id,
                    "market_data_quality": e.market_data_quality,
                }
                for e in db.query(V4ShadowConfigEntry).filter_by(
                    shadow_decision_id=result.decision_id
                )
            ]
            observations = [
                {
                    "candidate_id": o.candidate_id,
                    "phase": o.phase,
                    "status": o.status,
                    "net_executable_value": str(o.net_executable_value),
                    "unique_contract_count": o.unique_contract_count,
                    "earliest_leg_observed_at": o.earliest_leg_observed_at,
                    "latest_leg_observed_at": o.latest_leg_observed_at,
                    "max_leg_timestamp_skew_seconds": str(o.max_leg_timestamp_skew_seconds),
                    "market_data_quality": o.market_data_quality,
                    "legs": (o.legs_json or {}).get("legs"),
                }
                for o in db.query(V4ShadowCandidateObservation).filter_by(
                    shadow_decision_id=result.decision_id
                )
            ]
            decision_row = db.get(V4ShadowDecision, result.decision_id)
            assert decision_row is not None
            decision_versions = {
                "engine": decision_row.engine_version,
                "ranking": decision_row.ranking_version,
                "valuation": decision_row.valuation_version,
                "scenario_grid": decision_row.scenario_grid_version,
                "timing_policy": decision_row.decision_timing_policy_version,
            }
        else:
            decision_versions = {}
    finally:
        outer.rollback()  # ZERO-WRITE: everything above is discarded.
    timings["total_ms"] = (time.monotonic() - t0) * 1000

    latency = getattr(assembly, "latency", None)
    unique_selected = sorted(
        {c["rank_1_candidate_id"] for c in configs if c["rank_1_candidate_id"]}
    )
    return {
        "notice": DRY_RUN_NOTICE,
        "ticker": ticker.upper(),
        "status": result.status if result else "FAILED",
        "reason": result.reason if result else None,
        "view": None
        if view is None
        else {
            "direction": view.direction,
            "volatility_view": view.volatility_view,
            "expected_move_intent": view.expected_move_intent,
            "confidence": view.confidence,
            "llm_provider": view.llm_provider,
            "llm_model": view.llm_model,
        },
        "view_note": view_note,
        "transient_calendar_event": transient_calendar_event,
        "assembly": summarize_assembly(assembly),
        "request_budget": {
            "underlying_quotes": assembly.budget.underlying_quotes,
            "metadata_calls": assembly.budget.metadata_calls,
            "chain_discovery_calls": assembly.budget.chain_discovery_calls,
            "selected_leg_quote_calls": assembly.budget.selected_leg_quote_calls,
            "unique_contracts_quoted": assembly.budget.unique_contracts_quoted,
            "total": assembly.budget.total,
        },
        "stage_latency_ms": {
            **({k: float(v) for k, v in vars(latency).items()} if latency is not None else {}),
            **{k: round(v, 3) for k, v in timings.items()},
        },
        "versions": decision_versions,
        "candidate_count": result.candidate_count if result else 0,
        "rankable_count": result.rankable_count if result else 0,
        "configurations": configs,
        "unique_selected_candidates": unique_selected,
        "config_entries": entries,
        "candidate_observations": observations,
        "evidence_sweeps": {
            "decision_view_calls": 1 if view is not None else 0,
            "assembly_calls": 1,
            "quote_sweeps": assembly.budget.selected_leg_quote_calls,
            "configurations_evaluated": len(configs),
        },
        "persisted": False,
        "orders_placed": 0,
        "standardized_capital_note": str(Decimal("2000")),
    }


@router.get("/contract-resolution", response_model=None)
def contract_resolution_check(tws_provider: TwsProviderDep, symbol: str = "BF.B") -> dict:
    """Section 37 -- read-only class-share resolution through the shared,
    lifespan-owned provider (no diagnostic socket). Uses the SAME
    normalization the production path uses."""
    from datetime import date as _date  # noqa: PLC0415

    from providers.ibkr_tws_options import ibkr_symbol  # noqa: PLC0415

    if tws_provider is None:
        raise InvalidRequestError("no shared TWS provider on this process")
    out: dict = {"symbol": symbol, "ibkr_symbol": ibkr_symbol(symbol)}
    t = time.monotonic()
    try:
        quote = tws_provider.get_underlying_quote(symbol)
        out["underlying"] = (
            None
            if quote is None
            else {
                k: (
                    str(v)
                    if isinstance(v, Decimal)
                    else (v.isoformat() if hasattr(v, "isoformat") else v)
                )
                for k, v in vars(quote).items()
                if not k.startswith("_")
            }
        )
    except Exception as exc:  # noqa: BLE001
        out["underlying_error"] = f"{type(exc).__name__}: {exc}"
    out["underlying_ms"] = round((time.monotonic() - t) * 1000, 1)
    t = time.monotonic()
    try:
        expirations = tws_provider.list_available_expirations(symbol, after=_date.today())
        out["expirations"] = [e.isoformat() for e in expirations][:12]
        out["expiration_count"] = len(expirations)
    except Exception as exc:  # noqa: BLE001
        out["expirations_error"] = f"{type(exc).__name__}: {exc}"
    out["secdef_ms"] = round((time.monotonic() - t) * 1000, 1)
    return out


@router.get("/option-tick-probe", response_model=None)
def option_tick_probe(
    tws_provider: TwsProviderDep, conids: str, seconds: float = 12.0
) -> dict:
    """READ-ONLY forensic probe for the 2026-09-04 V4 required-side
    settlement incident: hold one real streaming subscription per frozen
    conId on the SHARED production connection and report every RAW tick
    the wire actually delivered, so "bid missing" can be told apart from
    "IB sent a -1 no-bid sentinel that normalization dropped".

    Persists nothing, resolves nothing, places nothing."""
    from providers.ibkr_tws_options import _bare_option_contract  # noqa: PLC0415

    if tws_provider is None:
        raise InvalidRequestError("no shared TWS provider on this process")
    out: dict = {"probed_at": datetime.now(UTC).isoformat(), "contracts": []}
    for raw in conids.split(","):
        text = raw.strip()
        if not text.isdigit():
            continue
        conid = int(text)
        started = time.monotonic()
        entry: dict = {"conid": conid}
        try:
            result = tws_provider._connection.probe_market_data_ticks(
                _bare_option_contract(conid), seconds
            )
            ticks = result.pop("_raw_ticks", [])
            entry["raw_ticks"] = ticks
            entry["raw_tick_count"] = len(ticks)
            entry["normalized"] = {
                k: (str(v) if isinstance(v, Decimal) else v) for k, v in result.items()
            }
        except IBKRError as exc:
            entry["error"] = f"{type(exc).__name__}: {exc}"
        except Exception as exc:  # noqa: BLE001
            entry["error"] = f"{type(exc).__name__}: {exc}"
        entry["elapsed_ms"] = round((time.monotonic() - started) * 1000, 1)
        out["contracts"].append(entry)
    return out


@router.get("/session-close-probe", response_model=None)
def session_close_probe(
    tws_provider: TwsProviderDep, conids: str = "", symbols: str = "", session: str = ""
) -> dict:
    """READ-ONLY: what IBKR historical actually returns for a given session,
    per option conId and per underlying symbol. Used to establish whether a
    same-session closing mark is obtainable at all on this entitlement."""
    from datetime import date as _date  # noqa: PLC0415


    if tws_provider is None:
        raise InvalidRequestError("no shared TWS provider on this process")
    target = _date.fromisoformat(session) if session else _date.today()
    out: dict = {"session": target.isoformat(), "options": [], "underlyings": []}
    conn = tws_provider._connection  # noqa: SLF001
    conn.ensure_connected()
    for raw in [c.strip() for c in conids.split(",") if c.strip().isdigit()]:
        entry: dict = {"conid": int(raw)}
        try:
            close, source = tws_provider.get_session_close_with_source(int(raw), target)
            entry["close"] = None if close is None else str(close)
            entry["source"] = source
        except Exception as exc:  # noqa: BLE001
            entry["error"] = f"{type(exc).__name__}: {exc}"
        entry["last_error"] = conn._last_error  # noqa: SLF001
        out["options"].append(entry)

    for symbol in [s.strip().upper() for s in symbols.split(",") if s.strip()]:
        entry = {"symbol": symbol}
        try:
            entry["close"] = str(tws_provider.get_underlying_session_close(symbol, target))
        except Exception as exc:  # noqa: BLE001
            entry["error"] = f"{type(exc).__name__}: {exc}"
        entry["last_error"] = conn._last_error  # noqa: SLF001
        out["underlyings"].append(entry)
    return out
