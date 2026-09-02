from datetime import UTC, date, datetime

from models.company import Company
from models.document_chunk import EMBEDDING_DIM, DocumentChunk
from models.enums import FilingType
from models.filing import Filing
from rag.retrieval import RetrievalFilters, hybrid_search, keyword_search, vector_search

NOW = datetime.now(UTC)


def _zero_vector(hot_indices: dict[int, float]) -> list[float]:
    vec = [0.0] * EMBEDDING_DIM
    for idx, val in hot_indices.items():
        vec[idx] = val
    return vec


def _seed(db_session, ticker: str, accession: str) -> Company:
    company = Company(ticker=ticker, name=f"{ticker} Inc", cik=f"000{accession[-6:]}")
    db_session.add(company)
    db_session.flush()
    filing = Filing(
        company_id=company.id,
        filing_type=FilingType.FORM_10Q,
        filing_date=date(2025, 12, 18),
        accession_number=accession,
        source_url=f"https://example.com/{accession}.htm",
        retrieved_at=NOW,
    )
    db_session.add(filing)
    db_session.flush()
    return company, filing


def test_vector_search_orders_by_cosine_similarity(db_session):
    company, filing = _seed(db_session, "ZZTEST1", "TEST-0000000001")

    close = DocumentChunk(
        filing_id=filing.id,
        company_id=company.id,
        chunk_index=0,
        section="Item 7",
        text="HBM demand remained strong this quarter.",
        token_count=6,
        embedding=_zero_vector({0: 0.9, 1: 0.1}),
        embedding_model="test",
        retrieved_at=NOW,
    )
    far = DocumentChunk(
        filing_id=filing.id,
        company_id=company.id,
        chunk_index=1,
        section="Item 1A",
        text="Litigation risk factors are described below.",
        token_count=6,
        embedding=_zero_vector({1: 1.0}),
        embedding_model="test",
        retrieved_at=NOW,
    )
    db_session.add_all([close, far])
    db_session.flush()

    query_embedding = _zero_vector({0: 1.0})
    results = vector_search(db_session, query_embedding, k=2)

    assert results[0].text == close.text
    assert results[0].score > results[1].score


def test_vector_search_respects_company_filter(db_session):
    mu, mu_filing = _seed(db_session, "ZZTEST2", "TEST-0000000002")
    amd, amd_filing = _seed(db_session, "ZZTEST3", "TEST-0000000003")

    for company, filing, label in [(mu, mu_filing, "mu-chunk"), (amd, amd_filing, "amd-chunk")]:
        db_session.add(
            DocumentChunk(
                filing_id=filing.id,
                company_id=company.id,
                chunk_index=0,
                section=None,
                text=label,
                token_count=1,
                embedding=_zero_vector({0: 1.0}),
                embedding_model="test",
                retrieved_at=NOW,
            )
        )
    db_session.flush()

    results = vector_search(
        db_session,
        _zero_vector({0: 1.0}),
        filters=RetrievalFilters(company_ids=[mu.id]),
        k=10,
    )

    assert all(r.company_id == mu.id for r in results)
    assert any(r.text == "mu-chunk" for r in results)


def test_keyword_search_matches_full_text(db_session):
    company, filing = _seed(db_session, "ZZTEST4", "TEST-0000000004")
    db_session.add(
        DocumentChunk(
            filing_id=filing.id,
            company_id=company.id,
            chunk_index=0,
            section="Item 7",
            text="Gross margin improved due to favorable NAND pricing.",
            token_count=8,
            embedding=_zero_vector({}),
            embedding_model="test",
            retrieved_at=NOW,
        )
    )
    db_session.flush()

    # Scoped to this test's own company: the real, permanently-seeded corpus
    # (2,200+ real filing chunks) genuinely discusses NAND pricing too, so an
    # unscoped search would pick up unrelated real matches.
    results = keyword_search(
        db_session, "NAND pricing", filters=RetrievalFilters(company_ids=[company.id]), k=5
    )

    assert len(results) == 1
    assert "NAND" in results[0].text


def test_keyword_search_no_match_returns_empty(db_session):
    company, filing = _seed(db_session, "ZZTEST5", "TEST-0000000005")
    db_session.add(
        DocumentChunk(
            filing_id=filing.id,
            company_id=company.id,
            chunk_index=0,
            section=None,
            text="Completely unrelated sentence about staplers.",
            token_count=5,
            embedding=_zero_vector({}),
            embedding_model="test",
            retrieved_at=NOW,
        )
    )
    db_session.flush()

    results = keyword_search(
        db_session,
        "quantum blockchain synergy",
        filters=RetrievalFilters(company_ids=[company.id]),
        k=5,
    )
    assert results == []


def test_hybrid_search_combines_and_dedupes(db_session):
    company, filing = _seed(db_session, "ZZTEST6", "TEST-0000000006")
    # Matches both vector (hot dim 0) and keyword ("data center demand").
    both = DocumentChunk(
        filing_id=filing.id,
        company_id=company.id,
        chunk_index=0,
        section="Item 7",
        text="Data center demand accelerated in the quarter.",
        token_count=6,
        embedding=_zero_vector({0: 0.95}),
        embedding_model="test",
        retrieved_at=NOW,
    )
    # Matches only vector.
    vector_only = DocumentChunk(
        filing_id=filing.id,
        company_id=company.id,
        chunk_index=1,
        section="Item 1A",
        text="Unrelated risk language entirely.",
        token_count=4,
        embedding=_zero_vector({0: 0.8}),
        embedding_model="test",
        retrieved_at=NOW,
    )
    db_session.add_all([both, vector_only])
    db_session.flush()

    results = hybrid_search(
        db_session,
        query_text="data center demand",
        query_embedding=_zero_vector({0: 1.0}),
        filters=RetrievalFilters(company_ids=[company.id]),
        k=5,
    )

    result_texts = [r.text for r in results]
    assert both.text in result_texts
    # the chunk matching both signals should outrank the vector-only one
    assert result_texts.index(both.text) < result_texts.index(vector_only.text)


def test_hybrid_search_reranker_is_opt_in(db_session):
    """Phase 4 RAG hardening (2026-08-26), Section 25 -- RRF-only remains
    the default; a caller must explicitly pass a real reranker for it to
    run at all. Fake reranker only -- never the real network-downloading
    model (same convention as test_rag_reranking.py)."""
    company, filing = _seed(db_session, "ZZTEST7", "TEST-0000000007")
    first = DocumentChunk(
        filing_id=filing.id,
        company_id=company.id,
        chunk_index=0,
        section="Item 7",
        text="First real quarterly revenue commentary.",
        token_count=5,
        embedding=_zero_vector({0: 0.95}),
        embedding_model="test",
        retrieved_at=NOW,
    )
    second = DocumentChunk(
        filing_id=filing.id,
        company_id=company.id,
        chunk_index=1,
        section="Item 7",
        text="Second real quarterly revenue commentary.",
        token_count=5,
        embedding=_zero_vector({0: 0.9}),
        embedding_model="test",
        retrieved_at=NOW,
    )
    db_session.add_all([first, second])
    db_session.flush()

    default_results = hybrid_search(
        db_session,
        query_text="revenue",
        query_embedding=_zero_vector({0: 1.0}),
        filters=RetrievalFilters(company_ids=[company.id]),
        k=5,
    )
    assert [r.text for r in default_results] == [first.text, second.text]

    class _ReverseReranker:
        model_name = "fake"
        version = "test-v1"

        def score(self, query, documents):
            # Ascending index-based scores -> descending sort reverses order.
            return [float(i) for i in range(len(documents))]

    reranked_results = hybrid_search(
        db_session,
        query_text="revenue",
        query_embedding=_zero_vector({0: 1.0}),
        filters=RetrievalFilters(company_ids=[company.id]),
        k=5,
        reranker=_ReverseReranker(),
    )
    assert [r.text for r in reranked_results] == [second.text, first.text]
