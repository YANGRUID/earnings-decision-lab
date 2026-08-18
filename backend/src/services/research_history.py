"""Persistence for the two AI-generated research artifacts that used to
vanish the moment a user navigated away: AI Research answers
(AIResearchQuery) and AI Earnings Thesis generations (AIThesisVersion).
Both are append-only -- a row is written only after real generation
succeeds, and an AIThesisVersion is never overwritten (see
docs/limitations.md's point-in-time-research principle: yesterday's
thesis stays exactly as it was generated even after new evidence arrives).
"""

from dataclasses import dataclass

from sqlalchemy.orm import Session

from agents.types import AgentResponse
from models.ai_research_query import AIResearchQuery
from models.ai_thesis_version import AIThesisVersion
from models.company import Company
from rag.context import Citation
from services.earnings_thesis import EarningsThesisResult

DEFAULT_HISTORY_LIMIT = 20
MAX_HISTORY_LIMIT = 100


def _citations_to_json(citations: list[Citation]) -> list[dict]:
    return [
        {
            "marker": c.marker,
            "ticker": c.ticker,
            "filing_type": c.filing_type,
            "filing_date": c.filing_date.isoformat(),
            "section": c.section,
            "source_url": c.source_url,
        }
        for c in citations
    ]


def persist_research_query(
    db: Session,
    *,
    ticker: str | None,
    company: Company | None,
    provider: str,
    result: AgentResponse,
) -> AIResearchQuery:
    """Called only after ``orchestrator.run()`` has already returned a real
    answer -- never for a failed generation (see api/routers/research.py::
    research_query, which doesn't call this on an exception path)."""
    trace = result.trace
    row = AIResearchQuery(
        ticker=ticker,
        company_id=company.id if company else None,
        question=result.question,
        answer_markdown=result.answer,
        citations=_citations_to_json(result.citations),
        intent_category=trace.intent_category,
        planning_method=trace.planning_method,
        tool_calls=[
            {
                "tool_name": tc.tool_name,
                "arguments": tc.arguments,
                "success": tc.success,
                "duration_ms": tc.duration_ms,
                "summary": tc.summary,
                "error": tc.error,
                "query_description": tc.query_description,
            }
            for tc in trace.tool_calls
        ],
        verification_ran=trace.verification_ran,
        verification_supported=trace.verification_supported,
        revised=trace.revised,
        provider=provider,
        model=trace.model,
        total_input_tokens=trace.total_input_tokens,
        total_output_tokens=trace.total_output_tokens,
        estimated_cost_usd=trace.estimated_cost_usd,
        total_duration_ms=trace.total_duration_ms,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def list_research_queries(
    db: Session, *, ticker: str | None = None, limit: int = DEFAULT_HISTORY_LIMIT, offset: int = 0
) -> list[AIResearchQuery]:
    query = db.query(AIResearchQuery)
    if ticker is not None:
        query = query.filter(AIResearchQuery.ticker == ticker)
    return (
        # id.desc() as a tiebreaker: Postgres's now() (created_at's
        # server_default) is transaction-start time, not statement time, so
        # multiple rows written in one transaction can share an identical
        # created_at -- id (a monotonic serial) keeps "newest first" stable
        # and correct even then.
        query.order_by(AIResearchQuery.created_at.desc(), AIResearchQuery.id.desc())
        .offset(offset)
        .limit(min(limit, MAX_HISTORY_LIMIT))
        .all()
    )


def get_research_query(db: Session, query_id: int) -> AIResearchQuery | None:
    return db.get(AIResearchQuery, query_id)


def delete_research_query(db: Session, query_id: int) -> bool:
    row = db.get(AIResearchQuery, query_id)
    if row is None:
        return False
    db.delete(row)
    db.commit()
    return True


@dataclass(frozen=True)
class ThesisProvenance:
    """Which real snapshot rows a thesis generation actually used --
    threaded through from services/earnings_thesis.py so exact
    point-in-time provenance survives, not just free-text citations."""

    estimate_snapshot_id: int | None
    volatility_snapshot_id: int | None


def persist_thesis_version(
    db: Session,
    *,
    company: Company,
    provider: str,
    result: EarningsThesisResult,
    provenance: ThesisProvenance,
) -> AIThesisVersion:
    row = AIThesisVersion(
        company_id=company.id,
        business_context=result.thesis.business_context,
        historical_earnings_pattern=result.thesis.historical_earnings_pattern,
        guidance_trend=result.thesis.guidance_trend,
        key_risks=result.thesis.key_risks,
        market_setup=result.thesis.market_setup,
        disclaimer=result.thesis.disclaimer,
        citations=_citations_to_json(result.citations),
        provider=provider,
        model=result.model,
        earnings_estimate_snapshot_id=provenance.estimate_snapshot_id,
        volatility_snapshot_id=provenance.volatility_snapshot_id,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def list_thesis_versions(
    db: Session, company_id: int, *, limit: int = DEFAULT_HISTORY_LIMIT, offset: int = 0
) -> list[AIThesisVersion]:
    return (
        db.query(AIThesisVersion)
        .filter(AIThesisVersion.company_id == company_id)
        .order_by(AIThesisVersion.created_at.desc(), AIThesisVersion.id.desc())
        .offset(offset)
        .limit(min(limit, MAX_HISTORY_LIMIT))
        .all()
    )


def get_thesis_version(db: Session, version_id: int) -> AIThesisVersion | None:
    return db.get(AIThesisVersion, version_id)


def delete_thesis_version(db: Session, version_id: int) -> bool:
    row = db.get(AIThesisVersion, version_id)
    if row is None:
        return False
    db.delete(row)
    db.commit()
    return True
