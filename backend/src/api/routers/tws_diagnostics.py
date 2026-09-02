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

from fastapi import APIRouter

from analytics.decision.v4_4b_ranking import rank_candidates
from api.deps import TwsProviderDep
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

    strike, right, option_conid = min(
        contracts, key=lambda c: (abs(c[0] - underlying.price), c[1])
    )
    from providers.types import SelectedLeg  # noqa: PLC0415 -- local, diagnostic-only

    leg = SelectedLeg(
        strike=strike, option_type="call" if right == "C" else "put", action="buy"
    )
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
