import re

from models.company import Company
from providers.sec_edgar import SECEdgarProvider
from services.research_query_resolution import (
    extract_ticker_candidates,
    resolve_mentioned_companies,
)

_TICKERS_URL_PATTERN = re.compile(r"https://www\.sec\.gov/files/company_tickers\.json")

# Same reasoning as tests/test_services_symbol_resolution.py's own
# REAL_TICKERS_RESPONSE: fake "ZZQ..." tickers, never real ones, so this
# suite can't go flaky against whatever real companies already exist in
# the shared dev database.
REAL_TICKERS_RESPONSE = {
    "0": {"cik_str": 320193, "ticker": "ZZQNEW", "title": "ZZ New Ticker Test Co"},
}


def _edgar() -> SECEdgarProvider:
    return SECEdgarProvider(user_agent="Test test@example.com")


def test_extract_ticker_candidates_finds_all_caps_tokens():
    assert extract_ticker_candidates("What were INTU's last two earnings results?") == ["INTU"]
    assert extract_ticker_candidates("Compare CRM and NOW's latest earnings guidance") == [
        "CRM",
        "NOW",
    ]


def test_extract_ticker_candidates_filters_stopwords():
    assert extract_ticker_candidates("AND OR THE IS ARE WAS") == []
    assert extract_ticker_candidates("Ask the AI Research assistant about EPS") == []


def test_extract_ticker_candidates_dedupes_preserving_first_appearance_order():
    assert extract_ticker_candidates("INTU vs INTU, then AVGO, then INTU again") == [
        "INTU",
        "AVGO",
    ]


def test_resolve_mentioned_companies_with_no_candidates_returns_empty(db_session, httpx_mock):
    result = resolve_mentioned_companies(db_session, _edgar(), "hello there")
    assert result.resolved == []
    assert result.unresolved == []
    assert result.tickers == []


def test_resolve_mentioned_companies_prioritizes_explicit_ticker(db_session, httpx_mock):
    company = Company(ticker="ZZQEXP", name="ZZ Explicit Test Co", cik="0009999901")
    db_session.add(company)
    db_session.flush()

    # No httpx_mock response registered -- an already-known ticker must
    # resolve without any real SEC call.
    result = resolve_mentioned_companies(
        db_session, _edgar(), "What happened last quarter?", explicit_ticker="zzqexp"
    )

    assert result.tickers == ["ZZQEXP"]
    assert result.resolved[0].existing_company is not None
    assert result.resolved[0].existing_company.id == company.id


def test_resolve_mentioned_companies_resolves_existing_company_mentioned_in_text(
    db_session, httpx_mock
):
    company = Company(ticker="ZZQMEN", name="ZZ Mentioned Test Co", cik="0009999902")
    db_session.add(company)
    db_session.flush()

    result = resolve_mentioned_companies(db_session, _edgar(), "What is ZZQMEN's latest guidance?")

    assert result.tickers == ["ZZQMEN"]
    assert result.unresolved == []


def test_resolve_mentioned_companies_resolves_new_ticker_via_real_sec_lookup(
    db_session, httpx_mock
):
    httpx_mock.add_response(url=_TICKERS_URL_PATTERN, json=REAL_TICKERS_RESPONSE)

    result = resolve_mentioned_companies(db_session, _edgar(), "Tell me about ZZQNEW")

    assert result.tickers == ["ZZQNEW"]
    assert result.resolved[0].existing_company is None
    assert result.resolved[0].cik == "0000320193"


def test_resolve_mentioned_companies_reports_unresolved_for_a_real_lookup_that_fails(
    db_session, httpx_mock
):
    httpx_mock.add_response(url=_TICKERS_URL_PATTERN, json=REAL_TICKERS_RESPONSE)

    result = resolve_mentioned_companies(db_session, _edgar(), "Tell me about ZZQBAD")

    assert result.resolved == []
    assert result.unresolved == ["ZZQBAD"]


def test_resolve_mentioned_companies_bounds_new_ticker_lookups(db_session, httpx_mock):
    httpx_mock.add_response(url=_TICKERS_URL_PATTERN, json={}, is_reusable=True)

    question = "Compare ZZQAA ZZQBB ZZQCC ZZQDD ZZQEE all at once"
    result = resolve_mentioned_companies(db_session, _edgar(), question)

    # 5 distinct never-seen candidates named, but only 4 (the module's own
    # _MAX_NEW_TICKER_LOOKUPS) get a real network lookup attempt.
    assert len(result.unresolved) == 4
    assert result.resolved == []
    assert len(httpx_mock.get_requests()) == 4
