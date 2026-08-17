from decimal import Decimal

import pytest

from providers.sec_edgar import SECEdgarProvider

UA = "Earnings Decision Lab test@example.com"


def test_get_filing_html_returns_raw_unstripped_html(httpx_mock):
    httpx_mock.add_response(
        url="https://www.sec.gov/Archives/edgar/data/723125/000072312525000050/mu-10q.htm",
        text="<html><body><p>Real <b>filing</b> text.</p></body></html>",
    )
    provider = SECEdgarProvider(user_agent=UA)

    html = provider.get_filing_html(
        "https://www.sec.gov/Archives/edgar/data/723125/000072312525000050/mu-10q.htm"
    )

    assert "<b>filing</b>" in html  # unlike get_filing_text, tags are NOT stripped


def test_requires_real_contact_in_user_agent():
    with pytest.raises(ValueError):
        SECEdgarProvider(user_agent="not-a-contact")


def test_lookup_cik(httpx_mock):
    httpx_mock.add_response(
        url="https://www.sec.gov/files/company_tickers.json",
        json={
            "0": {"cik_str": 723125, "ticker": "MU", "title": "Micron Technology Inc"},
            "1": {"cik_str": 1045810, "ticker": "NVDA", "title": "NVIDIA CORP"},
        },
    )
    provider = SECEdgarProvider(user_agent=UA)
    assert provider.lookup_cik("mu") == "0000723125"


def test_lookup_cik_not_found(httpx_mock):
    httpx_mock.add_response(
        url="https://www.sec.gov/files/company_tickers.json",
        json={"0": {"cik_str": 723125, "ticker": "MU", "title": "Micron Technology Inc"}},
    )
    provider = SECEdgarProvider(user_agent=UA)
    assert provider.lookup_cik("ZZZZ") is None


def test_search_filings_filters_by_form(httpx_mock):
    httpx_mock.add_response(
        url="https://data.sec.gov/submissions/CIK0000723125.json",
        json={
            "name": "Micron Technology Inc",
            "filings": {
                "recent": {
                    "form": ["10-Q", "8-K", "10-K"],
                    "filingDate": ["2025-07-01", "2025-06-15", "2025-10-01"],
                    "accessionNumber": [
                        "0000723125-25-000050",
                        "0000723125-25-000040",
                        "0000723125-25-000070",
                    ],
                    "primaryDocument": ["mu-10q.htm", "mu-8k.htm", "mu-10k.htm"],
                    "reportDate": ["2025-05-29", "", "2025-08-28"],
                }
            },
        },
    )
    provider = SECEdgarProvider(user_agent=UA)
    filings = provider.search_filings("0000723125", filing_types=["10-Q", "10-K"])

    assert [f.filing_type for f in filings] == ["10-Q", "10-K"]
    assert filings[0].accession_number == "0000723125-25-000050"
    assert filings[0].source_url == (
        "https://www.sec.gov/Archives/edgar/data/723125/000072312525000050/mu-10q.htm"
    )


def test_get_company_facts_extracts_eps_and_revenue(httpx_mock):
    httpx_mock.add_response(
        url="https://data.sec.gov/api/xbrl/companyfacts/CIK0000723125.json",
        json={
            "entityName": "Micron Technology Inc",
            "facts": {
                "us-gaap": {
                    "EarningsPerShareDiluted": {
                        "units": {
                            "USD/shares": [
                                {
                                    "fy": 2025,
                                    "fp": "Q4",
                                    "val": 2.98,
                                    "filed": "2025-10-01",
                                    "end": "2025-08-28",
                                    "accn": "0000723125-25-000070",
                                    "form": "10-K",
                                },
                                {
                                    # Non 10-Q/10-K entries (e.g. amendments) are skipped.
                                    "fy": 2025,
                                    "fp": "Q4",
                                    "val": 2.90,
                                    "filed": "2025-10-15",
                                    "end": "2025-08-28",
                                    "accn": "0000723125-25-000075",
                                    "form": "10-K/A",
                                },
                            ]
                        }
                    },
                    "Revenues": {
                        "units": {
                            "USD": [
                                {
                                    "fy": 2025,
                                    "fp": "Q4",
                                    "val": 11320000000,
                                    "filed": "2025-10-01",
                                    "end": "2025-08-28",
                                    "accn": "0000723125-25-000070",
                                    "form": "10-K",
                                }
                            ]
                        }
                    },
                }
            },
        },
    )
    provider = SECEdgarProvider(user_agent=UA)
    facts = provider.get_company_facts("0000723125")

    assert len(facts.eps_diluted) == 1
    assert facts.eps_diluted[0].value == Decimal("2.98")
    assert facts.eps_diluted[0].fiscal_period == "Q4"
    assert facts.revenues[0].value == Decimal("11320000000")
    assert facts.source_provider == "sec_edgar_xbrl"
