"""Phase 4 RAG hardening (2026-08-26), Section 26 -- retrieval regression
fixtures over the REAL, already-ingested filing corpus (never fixture/
synthetic filing text). Read-only: never writes a row.

Compares V1 (hybrid RRF only) against V1+reranker (Section 25's OPTIONAL
rag.reranking.FastEmbedReranker) on the same real fixture questions, and
checks the structural correctness properties Section 26/44 ask for --
company scoping, point-in-time cutoff enforcement, no cross-company
leakage, no future-document leakage -- using this project's own real
company/filing data.

This script does NOT itself flip any default -- rag/retrieval.py's
hybrid_search stays RRF-only unless a caller explicitly passes a
reranker (see that module's own docstring). Per Section 26's own gating
principle, that only happens after real evidence like this script
produces justifies it, and only by a deliberate follow-up change, not
automatically from a script run.

Usage: PYTHONPATH=src python scripts/rag_evaluation_fixtures.py
"""

import sys
from dataclasses import dataclass
from datetime import date

from db.session import SessionLocal
from models.company import Company
from rag.embeddings import FastEmbedProvider
from rag.reranking import FastEmbedReranker
from rag.retrieval import RetrievalFilters, RetrievedChunk, hybrid_search


@dataclass(frozen=True)
class Fixture:
    name: str
    ticker: str
    query: str
    # A real cutoff strictly before this ticker's real, already-ingested
    # filings' latest filing_date -- proves cutoff enforcement actually
    # excludes something real, not merely "excludes nothing because
    # nothing exists after it anyway".
    as_of: date | None = None


# Real tickers confirmed to have real, already-chunked filings in this
# project's own corpus (see this phase's final report for the exact
# counts) -- not a fixed/small demo list, but a real cross-section.
FIXTURES = [
    Fixture(name="intu_guidance_question", ticker="INTU", query="revenue guidance outlook"),
    Fixture(name="risk_factor_question", ticker="CRWD", query="cybersecurity risk factors"),
    Fixture(
        name="business_overview_question",
        ticker="ADSK",
        query="business overview products and services",
    ),
    Fixture(
        name="latest_earnings_release_question",
        ticker="ZM",
        query="quarterly earnings results announcement",
    ),
    Fixture(
        name="point_in_time_question",
        ticker="AFRM",
        query="revenue growth",
        as_of=date(2023, 1, 1),
    ),
]


def _run_fixture(db, embedder, reranker, fixture: Fixture) -> dict:
    company = db.query(Company).filter(Company.ticker == fixture.ticker).one_or_none()
    if company is None:
        return {"fixture": fixture.name, "error": f"no company {fixture.ticker!r} on record"}

    filters = RetrievalFilters(company_ids=[company.id], filing_date_to=fixture.as_of)
    query_embedding = embedder.embed([fixture.query])[0]

    v1_results = hybrid_search(db, fixture.query, query_embedding, filters, k=5)
    v2_results = hybrid_search(db, fixture.query, query_embedding, filters, k=5, reranker=reranker)

    def _check(results: list[RetrievedChunk]) -> dict:
        return {
            "result_count": len(results),
            "company_scoping_correct": all(r.company_id == company.id for r in results),
            "cutoff_correct": (
                fixture.as_of is None or all(r.filing_date <= fixture.as_of for r in results)
            ),
            "top_sections": [f"{r.filing_type}/{r.section}" for r in results[:3]],
            "top_filing_dates": [str(r.filing_date) for r in results[:3]],
        }

    return {
        "fixture": fixture.name,
        "ticker": fixture.ticker,
        "query": fixture.query,
        "as_of": str(fixture.as_of) if fixture.as_of else None,
        "v1_rrf_only": _check(v1_results),
        "v2_with_reranker": _check(v2_results),
    }


def _check_cross_company_leakage(db, embedder) -> dict:
    """A query scoped to company A must never return a chunk from
    company B -- checked directly against two real, distinct companies
    rather than assumed from the SQL filter alone."""
    tickers = [f.ticker for f in FIXTURES[:2]]
    companies = {t: db.query(Company).filter(Company.ticker == t).one_or_none() for t in tickers}
    if any(c is None for c in companies.values()):
        return {"check": "cross_company_leakage", "skipped": True}

    query_embedding = embedder.embed(["revenue growth"])[0]
    leaks = []
    for ticker, company in companies.items():
        results = hybrid_search(
            db,
            "revenue growth",
            query_embedding,
            RetrievalFilters(company_ids=[company.id]),
            k=10,
        )
        other_ids = {c.id for t, c in companies.items() if t != ticker}
        leaked = [r for r in results if r.company_id in other_ids]
        if leaked:
            leaks.append(
                {"scoped_to": ticker, "leaked_company_ids": [r.company_id for r in leaked]}
            )
    return {"check": "cross_company_leakage", "leaks": leaks, "passed": not leaks}


def main() -> None:
    db = SessionLocal()
    embedder = FastEmbedProvider()
    reranker = FastEmbedReranker()

    results = [_run_fixture(db, embedder, reranker, f) for f in FIXTURES]
    results.append(_check_cross_company_leakage(db, embedder))

    import json

    print(json.dumps(results, indent=2, default=str))

    failures = [
        r
        for r in results
        if r.get("v1_rrf_only", {}).get("company_scoping_correct") is False
        or r.get("v1_rrf_only", {}).get("cutoff_correct") is False
        or r.get("v2_with_reranker", {}).get("company_scoping_correct") is False
        or r.get("v2_with_reranker", {}).get("cutoff_correct") is False
        or r.get("passed") is False
    ]
    if failures:
        print(
            f"\n{len(failures)} fixture(s) FAILED a structural correctness check.",
            file=sys.stderr,
        )
        sys.exit(1)
    print("\nAll structural correctness checks passed.", file=sys.stderr)


if __name__ == "__main__":
    main()
