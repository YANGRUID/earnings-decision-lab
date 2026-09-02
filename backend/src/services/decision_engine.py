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
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from agents.tools.earnings_history import EarningsHistoryArgs, EarningsHistoryTool
from agents.tools.filings_search import FilingsSearchArgs, FilingsSearchTool
from agents.tools.guidance_comparison import GuidanceComparisonArgs, GuidanceComparisonTool
from agents.tools.types import ToolOutcome
from analytics.decision.budget import BudgetFit, filter_and_size_by_budget
from analytics.decision.confidence import ConfidenceComponents, compute_confidence
from analytics.decision.reasoning import (
    build_expiration_bullets,
    build_risk_bullets,
    build_risk_profile_fit_bullets,
    build_strike_bullets,
    build_why_bullets,
    build_why_not_alternative_bullets,
)
from analytics.decision.risk_profile import (
    MIN_BID_ASK_COVERAGE,
    default_max_risk_utilization_pct,
    default_risk_profile_from_preference,
    filter_candidates_by_risk_profile,
    meets_liquidity_gate,
)
from analytics.decision.strategy_scoring import ViewRankedStrategy, rank_candidates_for_view
from analytics.options.payoff import OptionLeg, StrategyAnalysis
from analytics.options.strategy_candidates import StrategyCandidate
from core.config import get_settings
from models.company import Company
from models.enums import (
    DecisionDirection,
    DecisionVolatilityView,
    RiskProfile,
    StrategyRiskPreference,
)
from prompts.decision_view import SYSTEM_PROMPT, build_user_prompt
from providers.factory import get_options_provider
from providers.types import OptionQuote
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
    PricingSnapshotSelection,
    compute_and_persist_volatility_snapshot,
    compute_options_market_state,
    get_latest_volatility_snapshot,
)
from services.options_reconstruction import resolve_best_actionable_option_market
from services.provider_settings import get_app_provider_settings, get_strategy_risk_preference
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
    budget_fit: BudgetFit | None = None
    # Options Decision Engine V3 Part G -- see analytics/decision/reasoning.py.
    why_expiration: list[str] = field(default_factory=list)
    why_strikes: list[str] = field(default_factory=list)
    why_risk_profile: list[str] = field(default_factory=list)
    # Only ever populated on the #1 recommendation, comparing it against
    # alternative #2 -- never on the alternatives themselves.
    why_not_alternative: list[str] = field(default_factory=list)


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
    risk_profile: RiskProfile
    recommended: ScoredStrategy | None
    alternatives: list[ScoredStrategy]
    trade_budget: Decimal | None = None
    risk_cap: Decimal | None = None
    risk_cap_is_percent: bool = False
    budget_infeasible_minimum: Decimal | None = None
    # Phase 14.12: set only when zero real strategy candidates existed
    # *before* budget filtering ran at all -- i.e. the real options market
    # itself had nothing computable (no chain, contracts-only, stale
    # snapshot), never a budget question. Lets the UI show "no market data"
    # instead of "not feasible for $X budget" in that case -- budget must
    # never determine whether market data exists.
    no_market_data_reason: str | None = None
    # Phase 4 reproducibility hardening (2026-08-26), Section 7 -- the
    # exact quotes candidate generation was built from (``selection.quotes``
    # at the point ``generate_strategy_candidates`` was called), kept
    # around only so a caller freezing an immutable snapshot can look up
    # each recommended leg's real ``external_contract_id`` by
    # (strike, option_type) after the fact -- never used to change which
    # candidate was selected or how it was scored (that already happened
    # by the time this field is populated).
    option_quotes: list[OptionQuote] = field(default_factory=list)


def leg_to_dict(
    leg: OptionLeg,
    *,
    expiration: date | None = None,
    external_contract_id: str | None = None,
) -> dict:
    """``expiration``/``external_contract_id`` are Phase 4 reproducibility
    hardening additions (2026-08-26), Section 7 -- optional and default to
    None so every existing caller (services/decision_history.py, which
    persists the older, mutable ``ai_decision_version`` table this phase
    does not touch) keeps producing exactly the dict shape it always has.
    ``multiplier`` is always the standard US equity option multiplier this
    project already hardcodes at entry capture (see EntrySnapshot.multiplier)
    -- there is no per-leg concept of a non-standard multiplier anywhere in
    this codebase, so it's never conditional.
    """
    return {
        "option_type": leg.option_type.value,
        "action": leg.action.value,
        "strike": str(leg.strike),
        "premium": str(leg.premium),
        "quantity": leg.quantity,
        "multiplier": "100",
        "expiration": expiration.isoformat() if expiration is not None else None,
        "external_contract_id": external_contract_id,
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


def budget_fit_to_dict(fit: BudgetFit | None) -> dict | None:
    if fit is None:
        return None
    return {
        "trade_budget": str(fit.trade_budget),
        "risk_cap": str(fit.risk_cap) if fit.risk_cap is not None else None,
        "usable_risk_budget": str(fit.usable_risk_budget),
        "capital_at_risk_per_contract": (
            str(fit.capital_at_risk_per_contract)
            if fit.capital_at_risk_per_contract is not None
            else None
        ),
        "max_feasible_quantity": fit.max_feasible_quantity,
        "total_max_loss": str(fit.total_max_loss) if fit.total_max_loss is not None else None,
        "total_max_profit": (
            str(fit.total_max_profit) if fit.total_max_profit is not None else None
        ),
        "total_net_premium": (
            str(fit.total_net_premium) if fit.total_net_premium is not None else None
        ),
        "budget_utilization_pct": (
            str(fit.budget_utilization_pct) if fit.budget_utilization_pct is not None else None
        ),
        "remaining_budget": (
            str(fit.remaining_budget) if fit.remaining_budget is not None else None
        ),
        "feasible": fit.feasible,
        "minimum_required": (
            str(fit.minimum_required) if fit.minimum_required is not None else None
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
    trade_budget: Decimal | None = None,
    risk_cap: Decimal | None = None,
    risk_cap_is_percent: bool = False,
    risk_profile: RiskProfile | None = None,
    manual_expiration: date | None = None,
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

    ``risk_profile`` (Options Decision Engine V3 Part D) is selected PER
    DECISION, not a single global setting -- when omitted, defaults from
    the global StrategyRiskPreference app setting (see
    analytics/decision/risk_profile.py::default_risk_profile_from_preference)
    so existing callers that never pass it see unchanged behavior. Affects
    candidate eligibility (Conservative excludes single-leg long calls/
    puts), a liquidity gate (Conservative/Moderate require real minimum
    bid/ask coverage; a chain that fails it produces zero candidates, same
    as "no market data" -- never a silently degraded recommendation), and
    the default risk_cap utilization applied when ``trade_budget`` is
    given but ``risk_cap`` isn't.

    ``trade_budget`` (Phase 14.10 Part G), when given, restricts the
    recommended/alternative candidates to ones that are actually
    affordable at that budget (see analytics/decision/budget.py) --
    this is the real mechanism by which a $500 and a $10,000 budget can
    recommend genuinely different structures, not a cosmetic quantity
    label. When no real candidate fits, ``recommended`` is None and
    ``budget_infeasible_minimum`` reports the cheapest real structure
    this chain actually supports, so the owner knows what budget would
    be required instead of just seeing nothing.

    ``manual_expiration`` (Options Decision Engine V3 Part H/I), when
    given, fetches the real chain for EXACTLY that expiration live from
    the configured provider -- every contract, strike, and downstream
    strategy comes only from it, never silently substituted (Part 15).
    When omitted (Auto, the default), the existing market-data resolver
    (services/options_reconstruction.py::resolve_best_actionable_option_market)
    picks the current/previous-session snapshot exactly as before -- this
    is a real, stated scope boundary: Auto mode here is the resolver's
    own session-aware pick, not the separate multi-candidate Expiration
    Engine's scored comparison (see services/expiration_engine.py and the
    dedicated GET .../expirations endpoint for that comparison).
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
    now = datetime.now(UTC)
    earnings_date_for_resolution = estimate.estimated_report_date if estimate is not None else None
    if manual_expiration is not None:
        settings = get_settings()
        overrides = get_app_provider_settings(db)
        provider = get_options_provider(settings, overrides.options_primary, db)
        quotes = provider.get_option_chain(ticker, now, expiration=manual_expiration)
        priceable = [q for q in quotes if q.bid is not None or q.ask is not None or q.last_price]
        selection = PricingSnapshotSelection(
            quotes=quotes,
            tier="current_priceable" if priceable else ("contracts_only" if quotes else "none"),
            is_fallback=False,
            purpose="intraday",
            snapshot_timestamp=now,
        )
    else:
        # V4.1 source-coherence fix (2026-08-31): real decision generation
        # is the one caller that actually commits a trade, so it always
        # prefers a fresh live read over a same-day-but-hours-old
        # persisted snapshot when the market is open -- see
        # resolve_best_actionable_option_market's own docstring for the
        # real DY evidence this fixes. Every other caller of this shared
        # resolver (Strategy Lab, Upcoming Earnings) is unaffected.
        resolution = resolve_best_actionable_option_market(
            db, company, now, earnings_date_for_resolution, force_live_refresh=True
        )
        selection = resolution.selection
    if selection.quotes and (
        volatility is None or volatility.snapshot_timestamp != selection.snapshot_timestamp
    ):
        computed = compute_and_persist_volatility_snapshot(
            db, company, earnings_date_for_resolution, selection=selection
        )
        if computed is not None:
            volatility = computed
    market_state = compute_options_market_state(selection.quotes, now, volatility, selection)

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
    effective_risk_profile = risk_profile or default_risk_profile_from_preference(risk_preference)
    target_earnings_date = (
        estimate.estimated_report_date
        if estimate is not None and estimate.estimated_report_date is not None
        else None
    )
    liquidity_gate_failed = not meets_liquidity_gate(
        effective_risk_profile, market_state.bid_ask_contract_count, market_state.contract_count
    )
    if market_state.actionability == "stale_research_only" or liquidity_gate_failed:
        # Phase 14.11 Part 3/4: a HARD GATE, not merely a label -- a
        # snapshot two or more real US trading sessions old must never
        # back a generated strategy recommendation. The AI's own
        # direction/volatility view above is still evidence-grounded and
        # kept; only the deterministic strategy pick is withheld, via the
        # same "no candidates -> no recommendation" path already used
        # when a chain simply doesn't exist yet (see DecisionResult
        # below: recommended stays None, never a guess). Options Decision
        # Engine V3 Part D: the effective Risk Profile's own liquidity
        # floor (analytics/decision/risk_profile.py) is a second, equally
        # real gate -- Conservative/Moderate must never rank candidates
        # built from a chain whose overall bid/ask coverage they'd
        # themselves call too thin.
        candidates: list[StrategyCandidate] = []
    else:
        candidates = generate_strategy_candidates(db, company, target_earnings_date, selection)
        candidates = filter_candidates_by_risk_profile(candidates, effective_risk_profile)

    implied_move_pct = volatility.implied_move_pct if volatility is not None else None
    # market_state.has_bid_ask means "at least one contract has a bid, an
    # ask, OR a last price" (see OptionsMarketState) -- real for gating
    # "is there any usable price at all", but wrong for a liquidity/quote-
    # quality claim: a reconstructed chain (Phase 14.13) can be 100%
    # last-price-only with zero real bid/ask and would still read True
    # there, wrongly scoring full Liquidity credit and claiming "Real
    # bid/ask quotes were available". bid_ask_contract_count counts
    # contracts with an actual two-sided market (both bid AND ask).
    has_real_bid_ask = market_state.bid_ask_contract_count > 0
    ranked_all = rank_candidates_for_view(
        candidates,
        direction=direction,
        volatility_view=volatility_view,
        implied_move_pct=implied_move_pct,
        historical_move_pcts=historical_moves,
        has_bid_ask=has_real_bid_ask,
        market_data_quality=market_state.market_data_quality,
        earnings_date=target_earnings_date,
        risk_profile=effective_risk_profile,
    )

    # Phase 14.12: captured *before* budget filtering runs -- zero real
    # candidates here means the real options market had nothing computable
    # (no chain, contracts-only, stale snapshot, no risk-preference-eligible
    # structure), which is categorically different from "candidates existed
    # but didn't fit the budget." Budget must never determine whether market
    # data exists (see api/routers/research.py's equivalent hard gate) --
    # this is what lets the UI show the right one of those two messages.
    if ranked_all:
        no_market_data_reason = None
    elif liquidity_gate_failed:
        threshold_pct = int(MIN_BID_ASK_COVERAGE[effective_risk_profile] * 100)  # type: ignore[operator]
        no_market_data_reason = (
            f"The real chain's bid/ask coverage doesn't meet the "
            f"{effective_risk_profile.value.title()} risk profile's minimum "
            f"({threshold_pct}% of contracts must carry a real two-sided quote)."
        )
    else:
        no_market_data_reason = market_state.reason

    # Options Decision Engine V3 Part D: when a trade_budget is given but
    # no explicit risk_cap, the effective Risk Profile's own default
    # utilization applies (Part 24) -- never silently unlimited (risk_cap
    # absent otherwise means "use the whole budget", see
    # analytics/decision/budget.py::usable_risk_budget). An explicit
    # risk_cap from the caller always wins.
    effective_risk_cap = risk_cap
    effective_risk_cap_is_percent = risk_cap_is_percent
    if trade_budget is not None and risk_cap is None:
        effective_risk_cap = default_max_risk_utilization_pct(effective_risk_profile)
        effective_risk_cap_is_percent = True

    ranked_all, budget_fits, budget_infeasible_minimum = filter_and_size_by_budget(
        ranked_all,
        trade_budget=trade_budget,
        risk_cap=effective_risk_cap,
        risk_cap_is_percent=effective_risk_cap_is_percent,
    )

    def _scored(r: ViewRankedStrategy) -> ScoredStrategy:
        why = build_why_bullets(
            r,
            direction=direction,
            implied_move_pct=implied_move_pct,
            has_bid_ask=has_real_bid_ask,
        )
        risks = build_risk_bullets(r)
        return ScoredStrategy(
            ranked=r,
            why=why,
            risks=risks,
            budget_fit=budget_fits.get(r.rank),
            why_expiration=build_expiration_bullets(r, target_earnings_date),
            why_strikes=build_strike_bullets(r),
            why_risk_profile=build_risk_profile_fit_bullets(r, effective_risk_profile),
        )

    recommended = _scored(ranked_all[0]) if ranked_all else None
    alternatives = [_scored(r) for r in ranked_all[1:3]]
    if recommended is not None and ranked_all[1:2]:
        why_not = build_why_not_alternative_bullets(ranked_all[0], ranked_all[1])
        recommended = ScoredStrategy(
            ranked=recommended.ranked,
            why=recommended.why,
            risks=recommended.risks,
            budget_fit=recommended.budget_fit,
            why_expiration=recommended.why_expiration,
            why_strikes=recommended.why_strikes,
            why_risk_profile=recommended.why_risk_profile,
            why_not_alternative=why_not,
        )

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
        risk_profile=effective_risk_profile,
        recommended=recommended,
        alternatives=alternatives,
        option_quotes=selection.quotes,
        trade_budget=trade_budget,
        risk_cap=effective_risk_cap,
        risk_cap_is_percent=effective_risk_cap_is_percent,
        budget_infeasible_minimum=budget_infeasible_minimum,
        no_market_data_reason=no_market_data_reason,
    )
