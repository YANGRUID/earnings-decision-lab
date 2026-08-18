"""Grounded AI Earnings Thesis (Phase 14). Gathers real evidence
deterministically (the same agent tools agents/orchestrator.py uses, plus
this project's own deterministic strategy generation/ranking), then asks
the LLM to synthesize prose from it -- the LLM explains and connects real
facts, it never invents a number, a filing excerpt, or a strategy
ranking. See schemas/thesis.py and prompts/earnings_thesis.py for the
contract this enforces, and PROHIBITED_CERTAINTY_PHRASES below for the
one part of that contract this module verifies itself rather than just
asking the LLM to follow -- a thesis is never returned if it still
contains profit-guarantee language after one bounded correction attempt.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from agents.tools.earnings_history import EarningsHistoryArgs, EarningsHistoryTool
from agents.tools.filings_search import FilingsSearchArgs, FilingsSearchTool
from agents.tools.guidance_comparison import GuidanceComparisonArgs, GuidanceComparisonTool
from agents.tools.types import ToolOutcome
from analytics.earnings.historical_moves import HistoricalMoveStats
from analytics.options.strategy_ranking import RankedStrategy, rank_strategy_candidates
from models.company import Company
from models.earnings_estimate_snapshot import EarningsEstimateSnapshot
from models.volatility_snapshot import VolatilitySnapshot
from prompts.earnings_thesis import SYSTEM_PROMPT, build_user_prompt
from rag.context import Citation
from rag.embeddings import EmbeddingProvider
from schemas.thesis import EarningsThesis
from services.historical_moves import get_historical_move_stats
from services.llm.base import LLMProvider
from services.llm.errors import LLMError
from services.llm.types import ChatMessage
from services.market_expectations import get_latest_earnings_estimate
from services.options_analytics import get_latest_volatility_snapshot
from services.strategy_generation import generate_strategy_candidates

# Substring match, case-insensitive. Deliberately specific collocations
# ("guaranteed profit") rather than bare words ("guaranteed", "risk-free",
# "no risk") -- a real live check against DeepSeek found that the bare-word
# version flags exactly the *responsible* disclaimer language this project
# wants ("no outcome is guaranteed", "this is not risk-free"), and
# "risk-free" alone is also a standard finance term (the risk-free rate)
# unrelated to promising a risk-free trade. Every phrase below asserts a
# positive, certain, profitable outcome -- something that cannot appear in
# a responsibly-hedged sentence, so a match here is never a false positive
# in the other direction.
PROHIBITED_CERTAINTY_PHRASES = (
    "guaranteed profit",
    "guaranteed return",
    "guaranteed gain",
    "guaranteed win",
    "guaranteed outcome",
    "guaranteed to profit",
    "guaranteed to succeed",
    "guaranteed to win",
    "sure thing",
    "certain profit",
    "definitely will profit",
    "cannot lose",
    "can't lose",
    "risk-free profit",
    "risk-free return",
    "risk-free gain",
    "no risk of loss",
    "eliminates all risk",
    "100% profit",
    "100% return",
    "100% win",
    "surefire",
    "sure-fire",
    "always profit",
    "always wins",
)


class ThesisGenerationError(Exception):
    pass


@dataclass(frozen=True)
class EarningsThesisResult:
    thesis: EarningsThesis
    citations: list[Citation]
    generated_at: datetime
    model: str
    # Real provenance: which specific snapshot rows this thesis was
    # grounded in, so a persisted version can later be compared against
    # the *current* latest snapshot to detect "newer research data is
    # available" -- never inferred from a fixed staleness window. None
    # when no such snapshot existed yet at generation time (see
    # _market_setup_block's own None-handling above).
    estimate_snapshot_id: int | None
    volatility_snapshot_id: int | None


def _assemble_thesis_evidence(
    outcomes: list[tuple[str, ToolOutcome]],
) -> tuple[str, list[Citation]]:
    """Same shape as agents.orchestrator's own evidence assembly, kept
    separate rather than imported: that one is keyed by ToolCallRecord
    (orchestrator-specific bookkeeping this deterministic, non-orchestrator
    call path doesn't produce), this one by a plain label string.
    """
    blocks: list[str] = []
    citations: list[Citation] = []
    for label, outcome in outcomes:
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
    return "\n\n".join(blocks), citations


def _market_setup_block(
    estimate: EarningsEstimateSnapshot | None,
    volatility: VolatilitySnapshot | None,
    move_stats: HistoricalMoveStats | None,
    ranked: list[RankedStrategy],
) -> str:
    lines: list[str] = []

    if estimate is not None:
        lines.append(
            f"Next earnings estimate: EPS avg {estimate.eps_estimate_average}, revision trend "
            f"{estimate.eps_revision_direction}, estimated report date "
            f"{estimate.estimated_report_date} ({estimate.horizon} consensus, source "
            f"{estimate.source_provider})."
        )
    else:
        lines.append(
            "No real analyst-consensus estimate has been collected yet for the next earnings "
            "period."
        )

    if volatility is not None:
        lines.append(
            f"Options-implied move: {volatility.implied_move_pct} "
            f"(${volatility.implied_move_absolute}), ATM IV {volatility.atm_iv_near}, "
            f"expiration used {volatility.near_term_expiration}, method {volatility.method}."
        )
    else:
        lines.append(
            "No real options-implied move has been computed yet for the next earnings period."
        )

    if move_stats is not None:
        lines.append(
            f"Historical earnings-day moves ({move_stats.sample_size} past events): average "
            f"|move| {move_stats.average_abs_move_pct}, median {move_stats.median_abs_move_pct}, "
            f"largest {move_stats.largest_move_pct_signed}."
        )
    else:
        lines.append("No real historical earnings-day price-reaction data is on record yet.")

    if ranked:
        top = ranked[0]
        lines.append(
            f"Top-ranked deterministic strategy candidate out of {len(ranked)} ranked: "
            f"{top.candidate.category.value} -- {top.explanation}"
        )
    else:
        lines.append(
            "No deterministic strategy candidates are available yet (real options-chain or "
            "implied-move data is missing)."
        )

    return "\n".join(lines)


def _find_prohibited_phrases(thesis: EarningsThesis) -> list[str]:
    text = " ".join(
        [
            thesis.business_context,
            thesis.historical_earnings_pattern,
            thesis.guidance_trend,
            thesis.key_risks,
            thesis.market_setup,
            thesis.disclaimer,
        ]
    ).lower()
    return [p for p in PROHIBITED_CERTAINTY_PHRASES if p in text]


def _generate_and_enforce(llm: LLMProvider, messages: list[ChatMessage]) -> EarningsThesis:
    try:
        thesis = llm.generate_structured(messages, EarningsThesis, temperature=0.0, max_tokens=1400)
    except LLMError as exc:
        raise ThesisGenerationError(f"thesis generation failed: {exc}") from exc

    found = _find_prohibited_phrases(thesis)
    if not found:
        return thesis

    # One bounded correction attempt -- same pattern as the general agent
    # orchestrator's verification-triggered revision (agents/orchestrator.py).
    retry_messages = [
        *messages,
        ChatMessage(
            role="user",
            content=(
                f"Your previous draft used prohibited certainty/guarantee language: {found}. "
                "Rewrite every section without any such language -- describe the real evidence "
                "honestly, with no implication that any outcome is certain or guaranteed."
            ),
        ),
    ]
    try:
        thesis = llm.generate_structured(
            retry_messages, EarningsThesis, temperature=0.0, max_tokens=1400
        )
    except LLMError as exc:
        raise ThesisGenerationError(f"thesis regeneration failed: {exc}") from exc

    found_again = _find_prohibited_phrases(thesis)
    if found_again:
        raise ThesisGenerationError(
            "generated thesis still contained prohibited certainty language after one "
            f"correction attempt: {found_again} -- refusing to return it"
        )
    return thesis


def generate_earnings_thesis(
    db: Session, llm: LLMProvider, embedder: EmbeddingProvider, company: Company
) -> EarningsThesisResult:
    ticker = company.ticker

    history_outcome = EarningsHistoryTool(db).run(EarningsHistoryArgs(ticker=ticker, limit=8))
    business_outcome = FilingsSearchTool(db, embedder).run(
        FilingsSearchArgs(
            query=f"{ticker} business overview products segments recent results",
            ticker=ticker,
            k=5,
        )
    )
    risk_outcome = FilingsSearchTool(db, embedder).run(
        FilingsSearchArgs(query=f"{ticker} risk factors uncertainties", ticker=ticker, k=5)
    )
    guidance_outcome = GuidanceComparisonTool(db).run(GuidanceComparisonArgs(ticker=ticker))

    evidence_text, citations = _assemble_thesis_evidence(
        [
            ("historical_earnings", history_outcome),
            ("business_context_filings", business_outcome),
            ("risk_factor_filings", risk_outcome),
            ("guidance_comparison", guidance_outcome),
        ]
    )

    estimate = get_latest_earnings_estimate(db, company.id)
    volatility = get_latest_volatility_snapshot(db, company.id)
    move_stats = get_historical_move_stats(db, company.id)

    ranked: list[RankedStrategy] = []
    if volatility is not None and volatility.target_earnings_date is not None:
        candidates = generate_strategy_candidates(db, company, volatility.target_earnings_date)
        ranked = rank_strategy_candidates(candidates, ticker, volatility.implied_move_pct)

    evidence_text = (
        f"{evidence_text}\n\n### market_setup_facts\n"
        f"{_market_setup_block(estimate, volatility, move_stats, ranked)}"
    )

    messages = [
        ChatMessage(role="system", content=SYSTEM_PROMPT),
        ChatMessage(role="user", content=build_user_prompt(ticker, evidence_text)),
    ]
    thesis = _generate_and_enforce(llm, messages)

    return EarningsThesisResult(
        thesis=thesis,
        citations=citations,
        generated_at=datetime.now(UTC),
        model=llm.model,
        estimate_snapshot_id=estimate.id if estimate is not None else None,
        volatility_snapshot_id=volatility.id if volatility is not None else None,
    )
