"""Real SEC EDGAR adapter — free, official, no API key.

Endpoints used (all documented at https://www.sec.gov/os/webmaster-faq#developers
and https://www.sec.gov/edgar/sec-api-documentation):

- ``www.sec.gov/files/company_tickers.json``      ticker -> CIK lookup
- ``data.sec.gov/submissions/CIK##########.json``  recent filings list
- ``data.sec.gov/api/xbrl/companyfacts/CIK##.json`` reported XBRL facts (actual EPS/revenue)
- ``www.sec.gov/Archives/edgar/data/...``           primary filing documents

SEC's fair-access policy requires a descriptive User-Agent identifying the
requester (see ``SEC_EDGAR_USER_AGENT`` in core.config) and asks that
automated callers stay within a modest request rate; this adapter enforces a
minimum interval between requests and retries with backoff on 429/5xx.
"""

import re
import time
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from observability.http_client import new_http_client
from providers.base import FilingsProvider
from providers.types import CompanyFacts, CompanyFactValue, FilingMetadata

_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
_COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"

_EPS_TAGS = ("EarningsPerShareDiluted", "EarningsPerShareBasic")
_REVENUE_TAGS = (
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "Revenues",
)


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return False


class SECEdgarProvider(FilingsProvider):
    def __init__(
        self,
        user_agent: str,
        client: httpx.Client | None = None,
        min_request_interval_s: float = 0.2,
    ) -> None:
        if "@" not in user_agent:
            raise ValueError(
                "SEC EDGAR requires a real contact in the User-Agent, e.g. "
                "'Your Name your@email.com' — see SEC_EDGAR_USER_AGENT in .env"
            )
        self._client = client or new_http_client(timeout=10.0)
        self._headers = {"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"}
        self._min_interval = min_request_interval_s
        self._last_request_at = 0.0

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)

    @retry(
        retry=retry_if_exception(_retryable),
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
        reraise=True,
    )
    def _get_json(self, url: str) -> dict:
        self._throttle()
        response = self._client.get(url, headers=self._headers)
        self._last_request_at = time.monotonic()
        response.raise_for_status()
        return response.json()

    # --- CIK lookup -------------------------------------------------------

    def lookup_cik(self, ticker: str) -> str | None:
        data = self._get_json(_TICKERS_URL)
        ticker_upper = ticker.upper()
        for entry in data.values():
            if entry["ticker"].upper() == ticker_upper:
                return str(entry["cik_str"]).zfill(10)
        return None

    # --- FilingsProvider ----------------------------------------------------

    def search_filings(
        self, cik: str, filing_types: list[str], limit: int = 10
    ) -> list[FilingMetadata]:
        data = self._get_json(_SUBMISSIONS_URL.format(cik=cik))
        entity_name = data.get("name", "")
        recent = data["filings"]["recent"]
        n = len(recent["form"])
        results: list[FilingMetadata] = []
        for form, filing_date, accession, primary_doc, period, items in zip(
            recent["form"],
            recent["filingDate"],
            recent["accessionNumber"],
            recent["primaryDocument"],
            recent.get("reportDate", [None] * n),
            recent.get("items", [None] * n),
            strict=True,
        ):
            if form not in filing_types:
                continue
            accession_nodash = accession.replace("-", "")
            cik_nodash = str(int(cik))
            source_url = (
                f"https://www.sec.gov/Archives/edgar/data/{cik_nodash}/"
                f"{accession_nodash}/{primary_doc}"
            )
            results.append(
                FilingMetadata(
                    cik=cik,
                    company_name=entity_name,
                    filing_type=form,
                    filing_date=date.fromisoformat(filing_date),
                    accession_number=accession,
                    primary_document=primary_doc,
                    source_url=source_url,
                    fiscal_period=period or None,
                    items=items or None,
                    source_provider="sec_edgar",
                    retrieved_at=datetime.now(UTC),
                )
            )
            if len(results) >= limit:
                break
        return results

    def get_filing_text(self, filing: FilingMetadata) -> str:
        self._throttle()
        response = self._client.get(filing.source_url, headers=self._headers)
        self._last_request_at = time.monotonic()
        response.raise_for_status()
        return _strip_html(response.text)

    def get_filing_html(self, source_url: str) -> str:
        """Raw HTML, for real parsing (rag.parsing) rather than the naive
        regex stripper ``get_filing_text`` uses for its quick provenance
        check. Takes a URL directly (not a FilingMetadata) so callers can
        pass either a freshly-fetched FilingMetadata.source_url or an
        already-persisted Filing.source_url from the database."""
        self._throttle()
        response = self._client.get(source_url, headers=self._headers)
        self._last_request_at = time.monotonic()
        response.raise_for_status()
        return response.text

    # --- Bonus: real actual-results data via XBRL (not part of the generic
    # FilingsProvider interface — SEC-specific structured facts). ------------

    def get_company_facts(self, cik: str) -> CompanyFacts:
        data = self._get_json(_COMPANYFACTS_URL.format(cik=cik))
        facts = data.get("facts", {}).get("us-gaap", {})
        retrieved_at = datetime.now(UTC)
        return CompanyFacts(
            cik=cik,
            entity_name=data.get("entityName", ""),
            eps_diluted=_extract_fact_values(facts, _EPS_TAGS, unit="USD/shares"),
            revenues=_extract_fact_values(facts, _REVENUE_TAGS, unit="USD"),
            source_provider="sec_edgar_xbrl",
            retrieved_at=retrieved_at,
        )


def _extract_fact_values(facts: dict, tags: tuple[str, ...], unit: str) -> list[CompanyFactValue]:
    for tag in tags:
        tag_data = facts.get(tag)
        if not tag_data:
            continue
        units = tag_data.get("units", {})
        raw_values: list[dict] = units.get(unit) or next(iter(units.values()), [])
        values: list[CompanyFactValue] = []
        for entry in raw_values:
            # Only quarterly (10-Q) and annual (10-K) points tied to a fiscal
            # period — skip amendments' duplicate/segment-level entries.
            if entry.get("form") not in ("10-Q", "10-K") or not entry.get("fp"):
                continue
            try:
                value = Decimal(str(entry["val"]))
            except InvalidOperation:
                continue
            values.append(
                CompanyFactValue(
                    fiscal_year=entry["fy"],
                    fiscal_period=entry["fp"],
                    value=value,
                    unit=unit,
                    filed_date=date.fromisoformat(entry["filed"]),
                    end_date=date.fromisoformat(entry["end"]),
                    accession_number=entry["accn"],
                    form=entry["form"],
                )
            )
        if values:
            return values
    return []


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]+")
_BLANKLINES_RE = re.compile(r"\n{3,}")


def _strip_html(html: str) -> str:
    """Minimal HTML-to-text for Phase 1 provenance capture.

    This is intentionally naive (regex tag-stripping). Real structured
    parsing (sections, tables, exhibits) is implemented in Phase 5's
    ingestion pipeline — this function only needs to produce readable text
    for spot-checking that ingestion pulled the right document.
    """
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    text = _TAG_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text)
    text = _BLANKLINES_RE.sub("\n\n", text)
    return text.strip()
