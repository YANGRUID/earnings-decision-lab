"""AI Options Decision Engine (Phase 14.9). The explicit pipeline is:

    Evidence -> Earnings Thesis -> Directional/Volatility View ->
    Deterministic Candidate Generation -> Deterministic Risk/Reward Metrics
    -> Candidate Ranking -> AI Explanation -> Persisted Decision Record

The LLM classifies a direction/volatility view (schemas/decision.py) from
real evidence -- the same evidence-gathering this project already uses for
the Earnings Thesis (services/earnings_thesis.py), reusing the latest
non-stale thesis when one exists rather than regenerating it. Every
strategy candidate, its score breakdown, and its "why"/"risks" bullets are
then computed entirely deterministically (analytics/decision/*) from that
view -- the LLM never invents an option price, a payoff number, or a
score. See services/decision_history.py for persistence.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from agents.tools.earnings_history import EarningsHistoryArgs, EarningsHistoryTool
from agents.tools.filings_search import FilingsSearchArgs, FilingsSearchTool
from agents.tools.guidance_comparison import GuidanceComparisonArgs, GuidanceComparisonTool
from agents.tools.types import ToolOutcome
from analytics.decision.confidence import ConfidenceComponents, compute_confidence
from analytics.decision.reasoning import build_risk_bullets, build_why_bullets
from analytics.decision.strategy_scoring import (
    ViewRankedStrategy,
    filter_candidates_by_risk_preference,
    rank_candidates_for_view,
)
from analytics.options.payoff import OptionLeg, StrategyAnalysis
from analytics.options.strategy_candidates import StrategyCandidate
from models.company import Company
from models.enums import DecisionDirection, DecisionVolatilityView, StrategyRiskPreference
from prompts.decision_view import SYSTEM_PROMPT, build_user_prompt
from rag.context import Citation
from rag.embeddings import EmbeddingProvider
from schemas.decision import DecisionView
from schemas.thesis import EarningsThesis
from services.earnings_thesis import (
    PROHIBITED_CERTAINTY_PHRASES,
    EarningsThesisResult,
    ThesisGenerationError,
    generate_earnings_thesis,
)
from services.historical_moves import get_historical_move_pcts
from services.llm.base import LLMProvider
from services.llm.errors import LLMError
from services.llm.types import ChatMessage
from services.market_expectations import get_latest_earnings_estimate
from services.options_analytics import (
    compute_options_market_state,
    get_latest_options_chain,
    get_latest_volatility_snapshot,
)
from services.provider_settings import get_strategy_risk_preference
from services.research_history import (
    ThesisProvenance,
    is_thesis_stale,
    list_thesis_versions,
    persist_thesis_version,
)
from services.strategy_generation import generate_strategy_candidates


class DecisionGenerationError(Exception):
    pass


@dataclass(frozen=True)
class ScoredStrategy:
    ranked: ViewRankedStrategy
    why: list[str]
    risks: list[str]


@dataclass(frozen=True)
class DecisionResult:
    view: DecisionView
    citations: list[Citation]
    confidence: ConfidenceComponents
    generated_at: datetime
    provider: str
    model: str
    thesis_version_id: int | None
    estimate_snapshot_id: int | None
    volatility_snapshot_id: int | None
    expiration: object | None  # date | None
    underlying_price: Decimal | None
    implied_move_pct: Decimal | None
    risk_preference: StrategyRiskPreference
    recommended: ScoredStrategy | None
    alternatives: list[ScoredStrategy]


def leg_to_dict(leg: OptionLeg) -> dict:
    return {
        "option_type": leg.option_type.value,
        "action": leg.action.value,
        "strike": str(leg.strike),
        "premium": str(leg.premium),
        "quantity": leg.quantity,
    }


def analysis_to_dict(analysis: StrategyAnalysis) -> dict:
    return {
        "net_premium": str(analysis.net_premium),
        "max_profit": str(analysis.max_profit) if analysis.max_profit is not None else None,
        "max_loss": str(analysis.max_loss) if analysis.max_loss is not None else None,
        "breakevens": [str(b) for b in analysis.breakevens],
        "return_on_risk": (
            str(analysis.return_on_risk) if analysis.return_on_risk is not None else None
        ),
    }


def _find_prohibited_phrases(view: DecisionView) -> list[str]:
    text = " ".join(
        [
            view.rationale,
            view.bull_case,
            view.bear_case,
            view.key_catalysts,
            view.key_risks,
            view.disclaimer,
        ]
    ).lower()
    return [p for p in PROHIBITED_CERTAINTY_PHRASES if p in text]


def _generate_and_enforce(llm: LLMProvider, messages: list[ChatMessage]) -> DecisionView:
    try:
        view = llm.generate_structured(messages, DecisionView, temperature=0.0, max_tokens=1200)
    except LLMError as exc:
        raise DecisionGenerationError(f"decision view generation failed: {exc}") from exc

    found = _find_prohibited_phrases(view)
    if not found:
        return view

    retry_messages = [
        *messages,
        ChatMessage(
            role="user",
            content=(
                f"Your previous draft used prohibited certainty/guarantee language: {found}. "
                "Rewrite every section without any such language."
            ),
        ),
    ]
    try:
        view = llm.generate_structured(
            retry_messages, DecisionView, temperature=0.0, max_tokens=1200
        )
    except LLMError as exc:
        raise DecisionGenerationError(f"decision view regeneration failed: {exc}") from exc

    found_again = _find_prohibited_phrases(view)
    if found_again:
        raise DecisionGenerationError(
            "generated decision view still contained prohibited certainty language after one "
            f"correction attempt: {found_again} -- refusing to return it"
        )
    return view


def _gather_evidence(
    db: Session, embedder: EmbeddingProvider, ticker: str
) -> tuple[str, list[Citation], int, int]:
    """Same four deterministic evidence tools the Earnings Thesis uses,
    gathered again here (rather than importing the thesis module's own
    private helper) so this pipeline stage never silently breaks if that
    one's internals change shape -- see services/earnings_thesis.py's own
    precedent for keeping evidence assembly duplicated per real call site.
    Returns (evidence_text, citations, success_count, total_count)."""
    outcomes: list[tuple[str, ToolOutcome]] = [
        (
            "historical_earnings",
            EarningsHistoryTool(db).run(EarningsHistoryArgs(ticker=ticker, limit=8)),
        ),
        (
            "business_context_filings",
            FilingsSearchTool(db, embedder).run(
                FilingsSearchArgs(
                    query=f"{ticker} business overview products segments recent results",
                    ticker=ticker,
                    k=5,
                )
            ),
        ),
        (
            "risk_factor_filings",
            FilingsSearchTool(db, embedder).run(
                FilingsSearchArgs(query=f"{ticker} risk factors uncertainties", ticker=ticker, k=5)
            ),
        ),
        (
            "guidance_comparison",
            GuidanceComparisonTool(db).run(GuidanceComparisonArgs(ticker=ticker)),
        ),
    ]

    blocks: list[str] = []
    citations: list[Citation] = []
    success_count = 0
    for label, outcome in outcomes:
        if outcome.success:
            success_count += 1
        if not outcome.success:
            blocks.append(f"### {label}\n{outcome.error or outcome.summary}")
            continue
        if outcome.citations:
            blocks.append(f"### {label}\n{outcome.summary}\n{outcome.data.get('context_text', '')}")
            citations.extend(outcome.citations)
        else:
            blocks.append(
                f"### {label}\n{outcome.summary}\nData: {json.dumps(outcome.data, default=str)}"
            )
    return "\n\n".join(blocks), citations, success_count, len(outcomes)


def _get_or_generate_thesis(
    db: Session, llm: LLMProvider, embedder: EmbeddingProvider, company: Company
) -> tuple[EarningsThesis, int | None]:
    """Reuses the latest non-stale persisted thesis for this company when
    one exists (see services/research_history.py::is_thesis_stale) --
    generating a fresh one only when none exists yet or the underlying
    snapshots have moved on. Avoids a redundant LLM call on every decision
    generation while keeping the pipeline's real "Earnings Thesis ->
    Decision View" ordering (see module docstring)."""
    existing = list_thesis_versions(db, company.id, limit=1)
    if existing and not is_thesis_stale(db, company, existing[0]):
        v = existing[0]
        thesis = EarningsThesis(
            business_context=v.business_context,
            historical_earnings_pattern=v.historical_earnings_pattern,
            guidance_trend=v.guidance_trend,
            key_risks=v.key_risks,
            market_setup=v.market_setup,
            disclaimer=v.disclaimer,
        )
        return thesis, v.id

    try:
        result: EarningsThesisResult = generate_earnings_thesis(db, llm, embedder, company)
    except ThesisGenerationError as exc:
        raise DecisionGenerationError(
            f"could not generate the required earnings thesis: {exc}"
        ) from exc

    provenance = ThesisProvenance(
        estimate_snapshot_id=result.estimate_snapshot_id,
        volatility_snapshot_id=result.volatility_snapshot_id,
    )
    persisted = persist_thesis_version(
        db, company=company, provider=llm.name, result=result, provenance=provenance
    )
    return result.thesis, persisted.id


def generate_decision(
    db: Session,
    llm: LLMProvider,
    embedder: EmbeddingProvider,
    company: Company,
    *,
    direction_override: DecisionDirection | None = None,
    volatility_view_override: DecisionVolatilityView | None = None,
) -> DecisionResult:
    """Runs the full pipeline for ``company`` and returns a real,
    generated-but-not-yet-persisted decision -- callers persist it (see
    services/decision_history.py::persist_decision) only after this
    returns successfully, matching this project's persist-only-on-success
    convention.

    When ``direction_override``/``volatility_view_override`` are given
    (Part K: manual view override), the LLM step is skipped for that
    field and the deterministic strategy scoring/ranking uses the
    owner-chosen view instead -- everything downstream of the view is
    identical either way. The final DecisionSource is decided by the
    caller (the router), not here.
    """
    ticker = company.ticker
    thesis, thesis_version_id = _get_or_generate_thesis(db, llm, embedder, company)

    evidence_text, evidence_citations, success_count, total_count = _gather_evidence(
        db, embedder, ticker
    )
    evidence_text = (
        f"{evidence_text}\n\n### earnings_thesis\n"
        f"business_context: {thesis.business_context}\n"
        f"historical_earnings_pattern: {thesis.historical_earnings_pattern}\n"
        f"guidance_trend: {thesis.guidance_trend}\n"
        f"key_risks: {thesis.key_risks}\n"
        f"market_setup: {thesis.market_setup}\n"
    )

    estimate = get_latest_earnings_estimate(db, company.id)
    volatility = get_latest_volatility_snapshot(db, company.id)
    raw_chain = get_latest_options_chain(db, company)
    market_state = compute_options_market_state(raw_chain, datetime.now(UTC), volatility)

    if direction_override is not None and volatility_view_override is not None:
        view = DecisionView(
            direction=direction_override.value,
            volatility_view=volatility_view_override.value,
            rationale="Direction and volatility view were manually set by the owner, overriding "
            "the AI's own classification for this generation.",
            bull_case=thesis.market_setup,
            bear_case=thesis.key_risks,
            key_catalysts=thesis.guidance_trend,
            key_risks=thesis.key_risks,
            disclaimer="This is not investment advice. No outcome is guaranteed, and the "
            "direction below was chosen manually, not by the AI.",
        )
    else:
        messages = [
            ChatMessage(role="system", content=SYSTEM_PROMPT),
            ChatMessage(role="user", content=build_user_prompt(ticker, evidence_text)),
        ]
        view = _generate_and_enforce(llm, messages)

    direction = DecisionDirection(view.direction)
    volatility_view = DecisionVolatilityView(view.volatility_view)

    historical_moves = get_historical_move_pcts(db, company.id)
    snapshot_age_minutes = market_state.snapshot_age_minutes
    confidence = compute_confidence(
        direction=direction,
        evidence_tool_success_count=success_count,
        evidence_tool_total=total_count,
        revision_direction=estimate.eps_revision_direction if estimate is not None else None,
        historical_move_pcts=historical_moves,
        snapshot_age_minutes=snapshot_age_minutes,
        chain_exists=market_state.chain_exists,
        implied_move_available=market_state.implied_move_available,
    )

    risk_preference = get_strategy_risk_preference(db)
    target_earnings_date = (
        estimate.estimated_report_date
        if estimate is not None and estimate.estimated_report_date is not None
        else None
    )
    candidates: list[StrategyCandidate] = generate_strategy_candidates(
        db, company, target_earnings_date
    )
    candidates = filter_candidates_by_risk_preference(candidates, risk_preference)

    implied_move_pct = volatility.implied_move_pct if volatility is not None else None
    ranked_all = rank_candidates_for_view(
        candidates,
        direction=direction,
        volatility_view=volatility_view,
        implied_move_pct=implied_move_pct,
        historical_move_pcts=historical_moves,
        has_bid_ask=market_state.has_bid_ask,
        market_data_quality=market_state.market_data_quality,
    )

    def _scored(r: ViewRankedStrategy) -> ScoredStrategy:
        why = build_why_bullets(
            r,
            direction=direction,
            implied_move_pct=implied_move_pct,
            has_bid_ask=market_state.has_bid_ask,
        )
        risks = build_risk_bullets(r)
        return ScoredStrategy(ranked=r, why=why, risks=risks)

    recommended = _scored(ranked_all[0]) if ranked_all else None
    alternatives = [_scored(r) for r in ranked_all[1:3]]

    underlying_price = candidates[0].underlying_price if candidates else None
    expiration = candidates[0].expiration if candidates else None

    return DecisionResult(
        view=view,
        citations=evidence_citations,
        confidence=confidence,
        generated_at=datetime.now(UTC),
        provider=llm.name,
        model=llm.model,
        thesis_version_id=thesis_version_id,
        estimate_snapshot_id=estimate.id if estimate is not None else None,
        volatility_snapshot_id=volatility.id if volatility is not None else None,
        expiration=expiration,
        underlying_price=underlying_price,
        implied_move_pct=implied_move_pct,
        risk_preference=risk_preference,
        recommended=recommended,
        alternatives=alternatives,
    )
