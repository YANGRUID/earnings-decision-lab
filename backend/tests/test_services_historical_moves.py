from datetime import UTC, date, datetime
from decimal import Decimal

from models.company import Company
from models.earnings_event import EarningsEvent
from models.price_reaction import PriceReaction
from services.historical_moves import get_historical_move_stats


def _seed_company(db_session, ticker: str = "ZZHIST1", cik: str = "0009999933") -> Company:
    company = Company(ticker=ticker, name="ZZ Historical Test Co", cik=cik)
    db_session.add(company)
    db_session.flush()
    return company


def _seed_event_with_reaction(
    db_session, company: Company, fiscal_quarter: int, next_day_move_pct: Decimal | None
) -> EarningsEvent:
    event = EarningsEvent(
        company_id=company.id,
        fiscal_year=2025,
        fiscal_quarter=fiscal_quarter,
        earnings_date=date(2025, fiscal_quarter * 3, 15),
    )
    db_session.add(event)
    db_session.flush()
    db_session.add(
        PriceReaction(
            earnings_event_id=event.id,
            next_day_move_pct=next_day_move_pct,
            source_provider="test",
            retrieved_at=datetime.now(UTC),
        )
    )
    db_session.flush()
    return event


def test_returns_none_when_no_other_reported_events_exist(db_session):
    company = _seed_company(db_session)
    event = _seed_event_with_reaction(db_session, company, 1, Decimal("0.05"))

    assert get_historical_move_stats(db_session, company.id, exclude_event_id=event.id) is None


def test_excludes_the_currently_viewed_event(db_session):
    company = _seed_company(db_session)
    older = _seed_event_with_reaction(db_session, company, 1, Decimal("-0.06"))
    current = _seed_event_with_reaction(db_session, company, 2, Decimal("0.99"))

    result = get_historical_move_stats(db_session, company.id, exclude_event_id=current.id)

    assert result is not None
    assert result.sample_size == 1
    assert result.largest_move_pct_signed == Decimal("-0.06")
    assert older.id != current.id


def test_excludes_events_with_no_move_recorded_yet(db_session):
    company = _seed_company(db_session)
    _seed_event_with_reaction(db_session, company, 1, Decimal("0.04"))
    no_move_yet = _seed_event_with_reaction(db_session, company, 2, None)
    current = _seed_event_with_reaction(db_session, company, 3, Decimal("0.20"))

    result = get_historical_move_stats(db_session, company.id, exclude_event_id=current.id)

    assert result is not None
    assert result.sample_size == 1
    assert no_move_yet.id != current.id


def test_only_includes_events_for_the_given_company(db_session):
    company_a = _seed_company(db_session, ticker="ZZHISTA", cik="0009999934")
    company_b = _seed_company(db_session, ticker="ZZHISTB", cik="0009999935")
    _seed_event_with_reaction(db_session, company_b, 1, Decimal("0.50"))
    current = _seed_event_with_reaction(db_session, company_a, 1, Decimal("0.01"))

    result = get_historical_move_stats(db_session, company_a.id, exclude_event_id=current.id)

    assert result is None
