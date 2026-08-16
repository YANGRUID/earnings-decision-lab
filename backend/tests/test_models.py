from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from models.company import Company
from models.earnings_event import EarningsEvent
from models.earnings_result import EarningsResult
from models.enums import AnnouncementTime


def test_company_earnings_event_result_relationship(db_session):
    company = Company(ticker="TEST1", name="Test Company One", cik="9999999991")
    db_session.add(company)
    db_session.flush()

    event = EarningsEvent(
        company_id=company.id,
        fiscal_year=2025,
        fiscal_quarter=4,
        earnings_date=date(2025, 9, 23),
        announcement_time=AnnouncementTime.AFTER_MARKET,
        date_confirmed=True,
    )
    db_session.add(event)
    db_session.flush()

    result = EarningsResult(
        earnings_event_id=event.id,
        actual_eps=Decimal("2.98"),
        actual_revenue=Decimal("11320000000"),
        source_provider="sec_edgar_xbrl",
        retrieved_at=datetime.now(UTC),
    )
    db_session.add(result)
    db_session.flush()

    db_session.refresh(event)
    assert event.company.ticker == "TEST1"
    assert event.result.actual_eps == Decimal("2.98")


def test_earnings_event_unique_constraint(db_session):
    company = Company(ticker="TEST2", name="Test Company Two", cik="9999999992")
    db_session.add(company)
    db_session.flush()

    db_session.add(EarningsEvent(company_id=company.id, fiscal_year=2026, fiscal_quarter=2))
    db_session.flush()

    db_session.add(EarningsEvent(company_id=company.id, fiscal_year=2026, fiscal_quarter=2))
    with pytest.raises(IntegrityError):
        db_session.flush()
