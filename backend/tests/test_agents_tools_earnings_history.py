from datetime import UTC, date, datetime
from decimal import Decimal

from agents.tools.earnings_history import EarningsHistoryArgs, EarningsHistoryTool
from models.company import Company
from models.earnings_event import EarningsEvent
from models.earnings_result import EarningsResult
from models.price_reaction import PriceReaction

NOW = datetime.now(UTC)


def test_returns_events_with_results_and_reactions(db_session):
    company = Company(ticker="ZZAGT1", name="ZZ Agent Test 1", cik="0009990001")
    db_session.add(company)
    db_session.flush()
    event = EarningsEvent(
        company_id=company.id, fiscal_year=2026, fiscal_quarter=3, earnings_date=date(2026, 6, 24)
    )
    db_session.add(event)
    db_session.flush()
    db_session.add(
        EarningsResult(
            earnings_event_id=event.id,
            actual_eps=Decimal("4.75"),
            actual_revenue=Decimal("9300000000"),
            source_provider="test",
            retrieved_at=NOW,
        )
    )
    db_session.add(
        PriceReaction(
            earnings_event_id=event.id,
            next_day_move_pct=Decimal("0.15"),
            five_day_move_pct=Decimal("0.10"),
            source_provider="test",
            retrieved_at=NOW,
        )
    )
    db_session.flush()

    tool = EarningsHistoryTool(db_session)
    outcome = tool.run(EarningsHistoryArgs(ticker="ZZAGT1"))

    assert outcome.success
    assert len(outcome.data["events"]) == 1
    event_data = outcome.data["events"][0]
    assert event_data["actual_eps"] == "4.750000"  # Numeric(18,6) column precision
    assert event_data["next_day_move_pct"] == "0.150000"
    assert outcome.query_description is not None


def test_unknown_ticker_returns_empty_not_error(db_session):
    tool = EarningsHistoryTool(db_session)
    outcome = tool.run(EarningsHistoryArgs(ticker="NOSUCHTICKER"))

    assert outcome.success
    assert outcome.data["events"] == []


def test_respects_limit(db_session):
    company = Company(ticker="ZZAGT2", name="ZZ Agent Test 2", cik="0009990002")
    db_session.add(company)
    db_session.flush()
    for q in range(1, 5):
        db_session.add(EarningsEvent(company_id=company.id, fiscal_year=2026, fiscal_quarter=q))
    db_session.flush()

    tool = EarningsHistoryTool(db_session)
    outcome = tool.run(EarningsHistoryArgs(ticker="ZZAGT2", limit=2))

    assert len(outcome.data["events"]) == 2
