import re

from models.company import Company
from providers.sec_edgar import SECEdgarProvider
from services.symbol_resolution import normalize_ticker, resolve_symbol

_TICKERS_URL_PATTERN = re.compile(r"https://www\.sec\.gov/files/company_tickers\.json")

# Real-shaped response per SEC's own company_tickers.json format. "ZZQNEW"
# (not a real ticker) rather than a real one on purpose: this project's
# shared dev database can carry real, already-researched Company rows for
# real tickers (accumulated by the live backend/scheduler, not by this
# test suite), which would make a "ticker the DB has never seen before"
# test flaky against whatever real companies happen to already exist.
REAL_TICKERS_RESPONSE = {
    "0": {"cik_str": 320193, "ticker": "ZZQNEW", "title": "ZZ New Ticker Test Co"},
    "1": {"cik_str": 1045810, "ticker": "NVDA", "title": "NVIDIA CORP"},
}


def _edgar() -> SECEdgarProvider:
    return SECEdgarProvider(user_agent="Test test@example.com")


def test_normalize_ticker_strips_and_uppercases():
    assert normalize_ticker("  aapl ") == "AAPL"


def test_empty_ticker_is_unsupported(db_session):
    result = resolve_symbol(db_session, _edgar(), "")
    assert result.supported is False
    assert "empty" in (result.reason or "")


def test_implausible_input_is_rejected_without_a_network_call(db_session, httpx_mock):
    # No httpx_mock response registered -- if this made a real call, the
    # test would fail on an unmocked request.
    result = resolve_symbol(db_session, _edgar(), "not a ticker!!!")
    assert result.supported is False
    assert result.cik is None


def test_already_researched_ticker_reuses_existing_company_without_a_sec_call(
    db_session, httpx_mock
):
    company = Company(ticker="ZZQSYM", name="ZZ Symbol Test Co", cik="0009999955")
    db_session.add(company)
    db_session.flush()

    # No httpx_mock response registered for company_tickers.json -- this
    # must not make a real SEC call for an already-known ticker.
    result = resolve_symbol(db_session, _edgar(), "zzqsym")

    assert result.supported is True
    assert result.existing_company is not None
    assert result.existing_company.id == company.id
    assert result.cik == "0009999955"


def test_new_real_ticker_resolves_via_sec_lookup(db_session, httpx_mock):
    httpx_mock.add_response(url=_TICKERS_URL_PATTERN, json=REAL_TICKERS_RESPONSE)

    result = resolve_symbol(db_session, _edgar(), "ZZQNEW")

    assert result.supported is True
    assert result.existing_company is None
    assert result.cik == "0000320193"


def test_unknown_ticker_not_in_sec_records_is_unsupported(db_session, httpx_mock):
    httpx_mock.add_response(url=_TICKERS_URL_PATTERN, json=REAL_TICKERS_RESPONSE)

    result = resolve_symbol(db_session, _edgar(), "ZZQQXX")

    assert result.supported is False
    assert result.cik is None
    assert "ZZQQXX" in (result.reason or "")
