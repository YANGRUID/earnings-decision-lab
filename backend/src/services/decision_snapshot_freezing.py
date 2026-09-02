"""Phase 4.3 -- turns a real, just-generated ``DecisionResult`` (see
services/decision_engine.py::generate_decision) into one immutable
``decision_snapshot`` row. See PHASE4.3_ARCHITECTURE_REVIEW.md for the
full design; this module is the "DecisionSnapshotService" that review
calls for, implemented as a plain-function module rather than a class --
matching this codebase's existing services/ convention (confirmed: no
existing service in this project is a class), exactly as
services/earnings_calendar_sync.py and services/earnings_eligibility.py
already did in Phase 4.2.

Mirrors services/decision_history.py::persist_decision's field-by-field
mapping pattern (the proven template for "how a DecisionResult becomes DB
columns"), targeting decision_snapshot's much wider Phase 4.3 schema
instead of ai_decision_version. Not a call to persist_decision itself --
a sibling function, since the two tables now have very different shapes
and different immutability rules.

Called at most once per (earnings_calendar_event, benchmark_portfolio)
pair -- decision_snapshot's own unique constraint enforces this at the
DB level; the caller (services/decision_pipeline.py) checks for an
existing row first so a routine, already-frozen event never even reaches
a live generate_decision() call.
"""

import logging
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.orm import Session

from analytics.decision.probability import EstimatedProbability, build_estimated_probability
from analytics.options.move_compatibility import MoveCompatibility, assess_move_compatibility
from models.benchmark_portfolio import BenchmarkPortfolio
from models.company import Company
from models.decision_snapshot import DecisionSnapshot
from models.earnings_calendar_event import EarningsCalendarEvent
from models.enums import DecisionVolatilityView
from models.volatility_snapshot import VolatilitySnapshot
from services.decision_engine import DecisionResult, leg_to_dict
from services.historical_moves import get_historical_move_pcts

# Version stamps for the deterministic engine / LLM prompt this module
# freezes -- no existing versioning mechanism for either exists elsewhere
# in this codebase (see decision_snapshot.py's own docstring). Bump by
# hand whenever a change to decision_engine.py's scoring/ranking logic or
# to its SYSTEM_PROMPT would make an old frozen row's numbers not
# directly comparable to a new one's.
ENGINE_VERSION = "options-decision-engine-v3"
PROMPT_VERSION = "v1"

# Phase 4.3 decision #3: V3's current Auto-mode expiration resolver
# (services/decision_engine.py's own internal
# resolve_best_actionable_option_market) is kept as-is, not replaced by
# the separate scored Expiration Engine -- this is the only value this
# column holds today. A future phase that actually wires in the scored
# engine (services/expiration_engine.py::resolve_auto_expiration) would
# introduce a second value here, not change this one's meaning.
EXPIRATION_SOURCE_V3_RESOLVER = "v3_auto_resolver"

log = logging.getLogger("services.decision_snapshot_freezing")

# iv_percentile thresholds for the volatility_regime classification --
# new to this phase, no existing V3 concept. Deliberately simple
# (tertile-like split), not a statistically fitted model.
_HIGH_IV_PERCENTILE = Decimal("70")
_LOW_IV_PERCENTILE = Decimal("30")


def _classify_volatility_regime(iv_percentile: Decimal | None) -> str | None:
    if iv_percentile is None:
        return None
    if iv_percentile >= _HIGH_IV_PERCENTILE:
        return "high"
    if iv_percentile <= _LOW_IV_PERCENTILE:
        return "low"
    return "normal"


@dataclass(frozen=True)
class FrozenProbability:
    """The one-time computation this phase performs instead of V3's
    live, read-time equivalent (api/routers/research.py's
    ``_historical_compatibility_for_decision`` + ``build_estimated_
    probability``) -- see PHASE4.3_ARCHITECTURE_REVIEW.md sec 2 for why
    freezing this, rather than recomputing it later, is the right call
    for a forward-tested snapshot."""

    estimated_probability: Decimal | None
    confidence_interval: dict | None
    historical_sample_size: int | None
    historical_compatibility: dict | None


def _freeze_probability(db: Session, company: Company, result: DecisionResult) -> FrozenProbability:
    recommended = result.recommended
    if recommended is None:
        return FrozenProbability(None, None, None, None)

    historical_moves = get_historical_move_pcts(db, company.id)
    compatibility: MoveCompatibility | None = assess_move_compatibility(
        recommended.ranked.candidate, historical_moves
    )
    estimated: EstimatedProbability | None = build_estimated_probability(compatibility)

    return FrozenProbability(
        estimated_probability=estimated.probability if estimated is not None else None,
        confidence_interval=(
            {
                "wilson_lower": str(estimated.wilson_lower)
                if estimated.wilson_lower is not None
                else None,
                "wilson_upper": str(estimated.wilson_upper)
                if estimated.wilson_upper is not None
                else None,
                "low_sample_confidence": estimated.low_sample_confidence,
            }
            if estimated is not None
            else None
        ),
        historical_sample_size=compatibility.sample_size if compatibility is not None else None,
        historical_compatibility=(
            {
                "method": compatibility.method,
                "sample_size": compatibility.sample_size,
                "requires_move_beyond_threshold": compatibility.requires_move_beyond_threshold,
                "required_move_pct": str(compatibility.required_move_pct),
                "compatible_count": compatibility.compatible_count,
                "compatible_pct": str(compatibility.compatible_pct),
            }
            if compatibility is not None
            else None
        ),
    )


def freeze_decision_snapshot(
    db: Session,
    *,
    calendar_event: EarningsCalendarEvent,
    portfolio: BenchmarkPortfolio,
    company: Company,
    result: DecisionResult,
) -> DecisionSnapshot:
    """Writes exactly one decision_snapshot row from an already-generated
    DecisionResult (see services/decision_pipeline.py, which calls
    generate_decision() and passes its result here) -- the row's own unique
    constraint (earnings_calendar_event_id, benchmark_portfolio_id) is
    the DB-level backstop against a duplicate; this function does not
    check for an existing row itself (see services/decision_pipeline.py,
    which checks first so a routine already-frozen event never reaches
    this far).

    Takes an already-generated ``result`` rather than calling
    generate_decision() itself -- mirrors persist_decision()'s exact
    signature shape (result: DecisionResult, not the ingredients to build
    one), which is what makes this function testable with a fixture
    DecisionResult, the same way test_services_decision_history.py tests
    persist_decision without a real LLM/provider call.
    """
    recommended = result.recommended
    probability = _freeze_probability(db, company, result)

    volatility_snapshot = (
        db.get(VolatilitySnapshot, result.volatility_snapshot_id)
        if result.volatility_snapshot_id is not None
        else None
    )

    # Phase 4 reproducibility hardening (2026-08-26), Section 7 -- matches
    # each recommended leg back to the real quote candidate generation
    # actually built it from. Never changes which candidate was selected
    # or how it was scored -- result.recommended was already decided
    # before this lookup is built; this only recovers metadata for legs
    # that selection already settled on.
    #
    # Post-official-run cleanup (2026-08-27), Section 5 -- keyed by
    # (expiration, strike, option_type), not (strike, option_type) alone.
    # result.option_quotes is the raw snapshot services/options_analytics.
    # py::select_pricing_snapshot read (every row sharing one
    # snapshot_timestamp, never filtered to one expiration -- see that
    # module's own multi-expiration term-structure use of the same rows),
    # so the same strike/right can be real at more than one expiration in
    # one decision's quotes. Unqualified (strike, option_type) risked
    # silently keying onto the WRONG expiration's contract id when that
    # happened; result.expiration -- the one expiration the recommended
    # strategy actually uses -- disambiguates it. (This is unrelated to
    # services/quote_telemetry.py's own (strike, option_type) matching:
    # that module only ever sees quotes already fetched for one explicit
    # expiration -- capture_benchmark_entry passes expiration=selected_
    # expiration to the provider -- so no such ambiguity can reach it.)
    #
    # A genuine same-key collision (two different real contract ids for
    # the same expiration/strike/right) would mean the snapshot itself is
    # corrupt -- never observed in real data. Logged and resolved to the
    # first one seen, deterministically, rather than raising: a metadata-
    # recovery ambiguity must not turn into a new way for decision
    # generation to fail (candidate selection above is already final by
    # this point).
    external_contract_id_by_leg: dict[tuple[object, Decimal, str], str] = {}
    for quote in result.option_quotes:
        if quote.external_contract_id is None:
            continue
        key = (quote.expiration_date, quote.strike, quote.option_type)
        existing = external_contract_id_by_leg.get(key)
        if existing is not None and existing != quote.external_contract_id:
            log.warning(
                "ambiguous option contract match for %s %s %s %s: "
                "snapshot carries distinct external_contract_id values "
                "%s and %s for the same expiration/strike/right -- "
                "keeping the first one seen",
                company.ticker,
                quote.expiration_date,
                quote.strike,
                quote.option_type,
                existing,
                quote.external_contract_id,
            )
            continue
        external_contract_id_by_leg[key] = quote.external_contract_id

    snapshot = DecisionSnapshot(
        earnings_calendar_event_id=calendar_event.id,
        benchmark_portfolio_id=portfolio.id,
        ticker=company.ticker,
        company_name=calendar_event.company_name,
        strategy_direction=result.view.direction,
        strategy_type=(
            recommended.ranked.candidate.category.value if recommended is not None else None
        ),
        ai_thesis_version_id=result.thesis_version_id,
        generated_at=result.generated_at,
        underlying_price=result.underlying_price,
        implied_volatility=(
            volatility_snapshot.atm_iv_near if volatility_snapshot is not None else None
        ),
        volatility_regime=_classify_volatility_regime(
            volatility_snapshot.iv_percentile if volatility_snapshot is not None else None
        ),
        option_snapshot_reference=result.volatility_snapshot_id,
        strategy_score=(recommended.ranked.score.total if recommended is not None else None),
        score_breakdown=(recommended.ranked.score.as_dict() if recommended is not None else None),
        selected_expiration=result.expiration,
        legs=(
            [
                leg_to_dict(
                    leg,
                    expiration=result.expiration,  # type: ignore[arg-type]
                    external_contract_id=external_contract_id_by_leg.get(
                        (result.expiration, leg.strike, leg.option_type.value)
                    ),
                )
                for leg in recommended.ranked.candidate.legs
            ]
            if recommended is not None
            else None
        ),
        estimated_probability=probability.estimated_probability,
        confidence_interval=probability.confidence_interval,
        historical_sample_size=probability.historical_sample_size,
        historical_compatibility=probability.historical_compatibility,
        why_this_strategy=(recommended.why if recommended is not None else None),
        why_this_expiration=(recommended.why_expiration if recommended is not None else None),
        why_these_strikes=(recommended.why_strikes if recommended is not None else None),
        why_not_alternatives=(recommended.why_not_alternative if recommended is not None else None),
        # Phase 4 reproducibility hardening (2026-08-26), Sections 3-6 --
        # these were already computed by generate_decision() for every
        # real decision attempt (even one with no recommended strategy),
        # just never frozen before now. Copied verbatim from ``result``,
        # never re-derived: result.risk_profile is already the effective
        # profile actually used (independent of the mutable
        # BenchmarkPortfolio.risk_profile reference), result.confidence is
        # already the deterministic evidence-confidence score, and
        # result.provider/result.model are already the real DecisionView-
        # generation LLM identity (see decision_snapshot.py's own column
        # docstrings for why this is always safe/honest to freeze here).
        volatility_view=DecisionVolatilityView(result.view.volatility_view),
        effective_risk_profile=result.risk_profile,
        deterministic_confidence_score=result.confidence.total,
        deterministic_confidence_breakdown=result.confidence.as_dict(),
        decision_llm_provider=result.provider,
        decision_llm_model=result.model,
        engine_version=ENGINE_VERSION,
        prompt_version=PROMPT_VERSION,
        expiration_source=EXPIRATION_SOURCE_V3_RESOLVER,
    )
    db.add(snapshot)
    db.flush()
    return snapshot
