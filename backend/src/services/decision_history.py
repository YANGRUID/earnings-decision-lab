"""Persistence for the AI Options Decision journal (Phase 14.9) --
append-only, exactly like AIThesisVersion (see services/research_history.py
for the sibling implementation this mirrors field-for-field). A decision
is persisted only after services.decision_engine.generate_decision
genuinely succeeds; a new generation is always a new row, matching this
project's point-in-time-research principle (see docs/limitations.md):
new evidence never rewrites what the system believed at an earlier
moment. Settlement (services/decision_settlement.py) is the one thing
that legitimately updates an existing row after the fact -- it fills in
real post-earnings outcome fields exactly once, never the view/rationale
fields themselves.
"""

from dataclasses import dataclass

from sqlalchemy.orm import Session

from models.ai_decision_version import AIDecisionVersion
from models.company import Company
from models.enums import DecisionSource
from rag.context import Citation
from services.decision_engine import DecisionResult, analysis_to_dict, leg_to_dict

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


def _strategy_json(scored) -> dict | None:  # noqa: ANN001 -- services.decision_engine.ScoredStrategy
    if scored is None:
        return None
    ranked = scored.ranked
    candidate = ranked.candidate
    return {
        "category": candidate.category.value,
        "legs": [leg_to_dict(leg) for leg in candidate.legs],
        "analysis": analysis_to_dict(candidate.analysis),
        "score": ranked.score.total,
        "score_components": ranked.score.as_dict(),
        "why": scored.why,
        "risks": scored.risks,
        "target_price": str(ranked.target_price) if ranked.target_price is not None else None,
        "payoff_at_target": (
            str(ranked.payoff_at_target) if ranked.payoff_at_target is not None else None
        ),
    }


def persist_decision(
    db: Session,
    *,
    company: Company,
    result: DecisionResult,
    decision_source: DecisionSource = DecisionSource.AI,
) -> AIDecisionVersion:
    """Called only after services.decision_engine.generate_decision has
    already returned a real result -- never for a failed generation."""
    recommended = result.recommended
    row = AIDecisionVersion(
        company_id=company.id,
        direction=result.view.direction,
        volatility_view=result.view.volatility_view,
        confidence_score=result.confidence.total,
        confidence_components=result.confidence.as_dict(),
        rationale=result.view.rationale,
        bull_case=result.view.bull_case,
        bear_case=result.view.bear_case,
        key_catalysts=result.view.key_catalysts,
        key_risks=result.view.key_risks,
        disclaimer=result.view.disclaimer,
        citations=_citations_to_json(result.citations),
        decision_source=decision_source,
        risk_preference=result.risk_preference.value,
        recommended_strategy_category=(
            recommended.ranked.candidate.category.value if recommended is not None else None
        ),
        recommended_strategy_legs=(
            [leg_to_dict(leg) for leg in recommended.ranked.candidate.legs]
            if recommended is not None
            else None
        ),
        recommended_strategy_analysis=(
            analysis_to_dict(recommended.ranked.candidate.analysis)
            if recommended is not None
            else None
        ),
        recommended_strategy_score=(
            recommended.ranked.score.total if recommended is not None else None
        ),
        recommended_strategy_score_components=(
            recommended.ranked.score.as_dict() if recommended is not None else None
        ),
        recommended_strategy_why=recommended.why if recommended is not None else None,
        recommended_strategy_risks=recommended.risks if recommended is not None else None,
        alternative_strategies=[_strategy_json(s) for s in result.alternatives],
        expiration=result.expiration,
        underlying_price=result.underlying_price,
        implied_move_pct=result.implied_move_pct,
        provider=result.provider,
        model=result.model,
        earnings_estimate_snapshot_id=result.estimate_snapshot_id,
        volatility_snapshot_id=result.volatility_snapshot_id,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def list_decisions(
    db: Session, company_id: int, *, limit: int = DEFAULT_HISTORY_LIMIT, offset: int = 0
) -> list[AIDecisionVersion]:
    return (
        db.query(AIDecisionVersion)
        .filter(AIDecisionVersion.company_id == company_id)
        .order_by(AIDecisionVersion.created_at.desc(), AIDecisionVersion.id.desc())
        .offset(offset)
        .limit(min(limit, MAX_HISTORY_LIMIT))
        .all()
    )


def get_decision(db: Session, decision_id: int) -> AIDecisionVersion | None:
    return db.get(AIDecisionVersion, decision_id)


def delete_decision(db: Session, decision_id: int) -> bool:
    row = db.get(AIDecisionVersion, decision_id)
    if row is None:
        return False
    db.delete(row)
    db.commit()
    return True


def mark_final(db: Session, decision_id: int) -> AIDecisionVersion | None:
    """Marks ``decision_id`` as the Final Decision for its company --
    unmarking any other decision that was previously final for that same
    company, so at most one decision per company is ever "final" at a
    time (see Phase 14.9 Part F section 22: the reference decision used
    for post-event track-record evaluation)."""
    row = db.get(AIDecisionVersion, decision_id)
    if row is None:
        return None
    db.query(AIDecisionVersion).filter(
        AIDecisionVersion.company_id == row.company_id, AIDecisionVersion.id != row.id
    ).update({AIDecisionVersion.is_final: False})
    row.is_final = True
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@dataclass(frozen=True)
class DecisionListFilters:
    ticker: str | None = None
    is_final_only: bool = False


def list_all_decisions(
    db: Session,
    *,
    filters: DecisionListFilters,
    limit: int = DEFAULT_HISTORY_LIMIT,
    offset: int = 0,
) -> list[AIDecisionVersion]:
    """Cross-company decision list, used by the global Track Record page
    (Phase 14.9 Part H) -- filters.ticker, when given, still goes through
    Company so this never trusts a caller-supplied company_id directly."""
    query = db.query(AIDecisionVersion).join(Company, AIDecisionVersion.company_id == Company.id)
    if filters.ticker is not None:
        query = query.filter(Company.ticker == filters.ticker)
    if filters.is_final_only:
        query = query.filter(AIDecisionVersion.is_final.is_(True))
    return (
        query.order_by(AIDecisionVersion.created_at.desc(), AIDecisionVersion.id.desc())
        .offset(offset)
        .limit(min(limit, MAX_HISTORY_LIMIT))
        .all()
    )
