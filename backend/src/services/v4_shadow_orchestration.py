"""V4.5 final wiring -- real per-event V4 shadow orchestration.

This is the piece the V4.5 report named as gap #1: the scheduler job used
to record only a clean run, and this module is what makes it actually
drive a per-event shadow decision end to end.

    authoritative earnings_calendar_event
        -> research-ready gate
        -> legal-timing gate (V3's OWN predicate, reused verbatim)
        -> DecisionView generation
        -> live candidate assembly (metadata -> geometry -> dedupe -> quote)
        -> V4.4A core + V4.4C stress valuation
        -> V4.4B ranking v1 (frozen, untouched)
        -> freeze V4ShadowDecision + ALL candidates + legs
        -> freeze rank #1 entry observation, or NO_ACTION

V3 PRIORITY AND ISOLATION (Section 4). Nothing here is imported by, or
reachable from, the official V3 path. Every event is processed inside its
own guarded scope: a failure on one event is recorded and the loop
continues, and a failure anywhere is recorded as shadow evidence rather
than raised, so the scheduler run -- and V3 -- are never taken down by V4.

LEGAL TIMING (Section 5). ``_due_for_decision_now`` is imported from
services/scheduler.py rather than reimplemented. That is deliberate: a
second, parallel definition could drift and silently hand V4 a different
(possibly earlier, possibly post-event) window than V3 used, which is the
one thing that would make the two cohorts incomparable.

NO LOOK-AHEAD (Section 8). This module imports no settlement, exit,
price-reaction, or realized-outcome module -- asserted structurally in
the V4.5 isolation tests.

SHARED CONNECTION (Section 9). The options provider is resolved through
providers/factory.py, which in this process returns the ONE shared
lifespan-owned IBKRTWSProvider. No provider is constructed here, no
connection is opened, and no client id is chosen.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol

from sqlalchemy.orm import Session

from core.config import Settings
from models.company import Company
from models.earnings_calendar_event import EarningsCalendarEvent
from models.v4_shadow import V4ShadowDecision, V4ShadowRunEvent
from providers.base import OptionsDataProvider
from services.v4_shadow import (
    ShadowCandidateInput,
    ShadowDecisionView,
    generate_shadow_decision,
    record_observation,
)
from services.v4_shadow_assembler import assemble_shadow_candidates

log = logging.getLogger("services.v4_shadow_orchestration")


class ViewGenerator(Protocol):
    """Produces the point-in-time market view for one ticker.

    Injected rather than hard-wired so tests can supply a deterministic
    view without an LLM call, and so the LLM's role stays bounded: it
    returns direction / volatility view / reasoning ONLY. It never sees
    or chooses a strategy, strike, expiration, rank, or size -- that is
    all deterministic Python downstream.
    """

    def __call__(
        self, db: Session, company: Company, event: EarningsCalendarEvent, as_of: datetime
    ) -> ShadowDecisionView | None: ...


@dataclass(frozen=True)
class ShadowEventOutcome:
    event_id: int
    ticker: str
    status: str
    reason: str | None = None
    decision_id: int | None = None
    candidate_count: int = 0


@dataclass(frozen=True)
class ShadowRunSummary:
    evaluated: int = 0
    ranked: int = 0
    no_action: int = 0
    already_generated: int = 0
    research_not_ready: int = 0
    failed: int = 0
    outcomes: tuple[ShadowEventOutcome, ...] = ()


def _record_event(
    db: Session,
    *,
    event_id: int | None,
    ticker: str | None,
    stage: str,
    category: str,
    message: str,
    retryable: bool = False,
) -> None:
    db.add(
        V4ShadowRunEvent(
            earnings_calendar_event_id=event_id,
            ticker=ticker,
            occurred_at=datetime.now(UTC),
            stage=stage,
            category=category,
            retryable=retryable,
            message=message,
        )
    )
    db.flush()


def _research_is_ready(db: Session, company: Company | None) -> tuple[bool, str]:
    """Section 7 -- a cheap, non-blocking readiness check.

    Deliberately does NOT trigger a synchronous SEC backfill, and does
    NOT wait for the research-worker. At the legal decision moment the
    answer is simply ready or not ready; producing a DecisionView from
    incomplete evidence would be worse than recording an honest
    RESEARCH_NOT_READY.
    """
    if company is None:
        return False, "no Company row exists for this calendar event yet"
    from models.ai_thesis_version import AIThesisVersion  # noqa: PLC0415 -- local, avoids cycle

    thesis = (
        db.query(AIThesisVersion)
        .filter_by(company_id=company.id)
        .order_by(AIThesisVersion.created_at.desc())
        .first()
    )
    if thesis is None:
        return False, "no AI thesis has been prepared for this company"
    return True, ""


def run_shadow_decisions_for_due_events(
    db: Session,
    settings: Settings,
    *,
    now: datetime,
    provider: OptionsDataProvider,
    view_generator: ViewGenerator,
    due_predicate,
    candidate_events: list[EarningsCalendarEvent],
) -> ShadowRunSummary:
    """Processes every genuinely due event. Never raises."""
    outcomes: list[ShadowEventOutcome] = []
    ranked = no_action = already = not_ready = failed = 0

    for event in candidate_events:
        ticker = event.symbol
        try:
            if not due_predicate(event, now):
                continue

            company = db.query(Company).filter_by(ticker=ticker).one_or_none()

            # Section 18 -- idempotency, checked before any work so a
            # retry costs nothing.
            existing = (
                db.query(V4ShadowDecision)
                .filter_by(
                    earnings_calendar_event_id=event.id,
                    engine_version=settings and "options-decision-engine-v4",
                )
                .first()
            )
            if existing is not None:
                already += 1
                outcomes.append(
                    ShadowEventOutcome(event.id, ticker, "ALREADY_GENERATED",
                                       "a shadow decision is already frozen for this event",
                                       existing.id)
                )
                continue

            ready, why = _research_is_ready(db, company)
            if not ready:
                not_ready += 1
                _record_event(
                    db, event_id=event.id, ticker=ticker, stage="research_gate",
                    category="RESEARCH_NOT_READY", message=why, retryable=True,
                )
                outcomes.append(
                    ShadowEventOutcome(event.id, ticker, "RESEARCH_NOT_READY", why)
                )
                continue

            assert company is not None  # guaranteed by _research_is_ready
            view = view_generator(db, company, event, now)
            if view is None:
                failed += 1
                _record_event(
                    db, event_id=event.id, ticker=ticker, stage="view",
                    category="VIEW_GENERATION_FAILED",
                    message="decision view generation returned nothing", retryable=True,
                )
                outcomes.append(
                    ShadowEventOutcome(event.id, ticker, "FAILED", "view generation failed")
                )
                continue

            assembly = assemble_shadow_candidates(
                provider=provider,
                ticker=ticker,
                as_of=now,
                direction=view.direction or "neutral",
                volatility_view=view.volatility_view,
                earnings_date=event.earnings_date,
            )
            if assembly.failure_category is not None:
                failed += 1
                _record_event(
                    db, event_id=event.id, ticker=ticker, stage="candidates",
                    category=assembly.failure_category,
                    message=assembly.failure_detail or "", retryable=True,
                )
                outcomes.append(
                    ShadowEventOutcome(event.id, ticker, "FAILED", assembly.failure_detail)
                )
                continue

            result = generate_shadow_decision(
                db,
                earnings_calendar_event_id=event.id,
                ticker=ticker,
                company_name=event.company_name,
                legal_decision_window_at=now,
                as_of=now,
                view=view,
                candidates=assembly.candidates,
                underlying_price=assembly.underlying_price,
                underlying_quote_at=assembly.underlying_quote_at,
                market_data_quality=assembly.market_data_quality,
                tws_request_count=assembly.budget.total,
                unique_contracts_quoted=assembly.budget.unique_contracts_quoted,
            )

            if result.status == "RANKED" and result.decision_id and result.rank_1_candidate_id:
                ranked += 1
                _freeze_entry_observation(
                    db,
                    decision_id=result.decision_id,
                    candidate_id=result.rank_1_candidate_id,
                    candidates=assembly.candidates,
                    observed_at=now,
                    market_data_quality=assembly.market_data_quality,
                )
            elif result.status == "NO_ACTION":
                no_action += 1
            elif result.status == "ALREADY_GENERATED":
                already += 1
            else:
                failed += 1

            outcomes.append(
                ShadowEventOutcome(
                    event.id, ticker, result.status, result.reason,
                    result.decision_id, result.candidate_count,
                )
            )

        except Exception as exc:  # noqa: BLE001 -- one event must never stop the run
            failed += 1
            log.error("v4 shadow decision failed for %s", ticker, exc_info=True)
            try:
                _record_event(
                    db, event_id=getattr(event, "id", None), ticker=ticker,
                    stage="shadow_decision", category="INTERNAL_ERROR",
                    message=f"{type(exc).__name__}: {exc}", retryable=True,
                )
            except Exception:  # noqa: BLE001 -- never let logging failure escape
                log.error("could not record v4 shadow failure event", exc_info=True)
            outcomes.append(
                ShadowEventOutcome(
                    getattr(event, "id", 0), ticker, "FAILED", f"{type(exc).__name__}: {exc}"
                )
            )

    return ShadowRunSummary(
        evaluated=len(outcomes),
        ranked=ranked,
        no_action=no_action,
        already_generated=already,
        research_not_ready=not_ready,
        failed=failed,
        outcomes=tuple(outcomes),
    )


def _freeze_entry_observation(
    db: Session,
    *,
    decision_id: int,
    candidate_id: str,
    candidates: list[ShadowCandidateInput],
    observed_at: datetime,
    market_data_quality: str | None,
) -> None:
    """Section 11 -- rank #1's executable entry observation.

    Uses the SAME leg quotes already frozen on the candidate, so the
    observation reflects exactly the prices the ranking was computed
    from. Re-quoting here would introduce a second, later observation
    and quietly break that correspondence.
    """
    source = next((c for c in candidates if c.candidate_id == candidate_id), None)
    if source is None:  # pragma: no cover -- defensive
        return
    record_observation(
        db,
        shadow_decision_id=decision_id,
        phase="ENTRY",
        candidate_id=candidate_id,
        legs=source.context.legs,
        observed_at=observed_at,
        market_data_quality=market_data_quality,
        leg_retrieved_at=source.leg_retrieved_at,
    )


def default_view_generator(
    db: Session, company: Company, event: EarningsCalendarEvent, as_of: datetime
) -> ShadowDecisionView | None:
    """Real DecisionView generation, with the LLM's role kept bounded.

    Reuses V3's OWN prompt and structured DecisionView schema -- the same
    direction/volatility-view semantics V4.2 was built to interpret -- so
    the shadow view is comparable rather than a parallel invention. Note
    it deliberately does NOT import services/decision_engine.py: that is
    an official V3 module, and V4 reaching into it would create exactly
    the coupling this project's isolation tests exist to prevent. Only
    the shared prompt and schema are reused.

    The model returns a VIEW only -- direction, volatility view, and
    reasoning. It never sees or chooses a strategy, strike, expiration,
    rank, or size; all of that is deterministic Python downstream.
    """
    from models.ai_thesis_version import AIThesisVersion  # noqa: PLC0415
    from prompts.decision_view import (  # noqa: PLC0415
        PROMPT_VERSION,
        SYSTEM_PROMPT,
        build_user_prompt,
    )
    from schemas.decision import DecisionView  # noqa: PLC0415
    from services.llm.errors import LLMError  # noqa: PLC0415
    from services.llm.factory import get_llm_provider  # noqa: PLC0415
    from services.llm.types import ChatMessage  # noqa: PLC0415

    # Section 8 -- point-in-time only. A thesis written AFTER the legal
    # decision moment must never inform it, so the boundary is enforced
    # in the query rather than assumed.
    thesis = (
        db.query(AIThesisVersion)
        .filter_by(company_id=company.id)
        .filter(AIThesisVersion.created_at <= as_of)
        .order_by(AIThesisVersion.created_at.desc())
        .first()
    )
    if thesis is None:
        return None

    evidence_text = "\n\n".join(
        part
        for part in (
            f"Business context: {thesis.business_context}",
            f"Historical earnings pattern: {thesis.historical_earnings_pattern}",
            f"Guidance trend: {thesis.guidance_trend}",
            f"Key risks: {thesis.key_risks}",
            f"Market setup: {thesis.market_setup}",
        )
        if part
    )

    from core.config import get_settings  # noqa: PLC0415

    settings = get_settings()
    try:
        llm = get_llm_provider(settings, db=db)
        view = llm.generate_structured(
            [
                ChatMessage(role="system", content=SYSTEM_PROMPT),
                ChatMessage(
                    role="user", content=build_user_prompt(company.ticker, evidence_text)
                ),
            ],
            DecisionView,
            temperature=0.0,
            max_tokens=1200,
        )
    except (LLMError, Exception):  # noqa: BLE001 -- V4 must never break V3
        log.error("v4 shadow decision view generation failed", exc_info=True)
        return None

    return ShadowDecisionView(
        direction=view.direction,
        volatility_view=view.volatility_view,
        expected_move_intent=None,
        confidence=None,
        reasoning=view.rationale,
        evidence_refs={"ai_thesis_version_id": thesis.id},
        llm_provider=settings.llm_provider,
        # Read off the constructed provider, which knows the model it
        # actually used -- Settings has no llm_model field, and an
        # owner override in the Settings UI can change the model
        # without any env var reflecting it.
        llm_model=getattr(llm, "model", None),
        prompt_version=PROMPT_VERSION,
    )


def utc_now() -> datetime:
    return datetime.now(UTC)


#: Exposed for the scheduler job so the numeric capital convention stays
#: in one place if it is ever needed for reporting.
ZERO = Decimal(0)
