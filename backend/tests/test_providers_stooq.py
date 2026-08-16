import re
from datetime import date

from providers.stooq import StooqMarketDataProvider

CSV_BODY = (
    "Date,Open,High,Low,Close,Volume\n"
    "2025-09-22,110.50,112.00,109.80,111.20,18234500\n"
    "2025-09-23,111.00,120.00,110.50,118.75,42891200\n"
)


def test_get_daily_bars_parses_csv(httpx_mock):
    httpx_mock.add_response(url=re.compile(r"https://stooq\.com/q/d/l/.*"), text=CSV_BODY)

    provider = StooqMarketDataProvider()
    bars = provider.get_daily_bars("MU", date(2025, 9, 22), date(2025, 9, 23))

    assert len(bars) == 2
    assert bars[1].trade_date == date(2025, 9, 23)
    assert bars[1].close == 118.75
    assert bars[0].source_provider == "stooq"


def test_get_daily_bars_handles_no_data(httpx_mock):
    httpx_mock.add_response(url=re.compile(r"https://stooq\.com/q/d/l/.*"), text="No data")

    provider = StooqMarketDataProvider()
    bars = provider.get_daily_bars("ZZZZ", date(2025, 9, 22), date(2025, 9, 23))

    assert bars == []
