from datetime import date
from decimal import Decimal

from ingestion.bootstrap_phase1 import _index_by_period
from providers.types import CompanyFactValue


def _fact(fiscal_year, fp, filed, val="1.00") -> CompanyFactValue:
    return CompanyFactValue(
        fiscal_year=fiscal_year,
        fiscal_period=fp,
        value=Decimal(val),
        unit="USD/shares",
        filed_date=date.fromisoformat(filed),
        end_date=date.fromisoformat(filed),
        accession_number="0000000000-00-000000",
        form="10-Q",
    )


def test_index_by_period_prefers_latest_filed_amendment():
    values = [
        _fact(2025, "Q1", "2025-04-01", val="1.00"),
        _fact(2025, "Q1", "2025-04-15", val="1.05"),  # later-filed amendment wins
        _fact(2025, "Q2", "2025-07-01", val="2.00"),
    ]

    indexed = _index_by_period(values)

    assert indexed[(2025, "Q1")].value == Decimal("1.05")
    assert indexed[(2025, "Q2")].value == Decimal("2.00")
    assert len(indexed) == 2
