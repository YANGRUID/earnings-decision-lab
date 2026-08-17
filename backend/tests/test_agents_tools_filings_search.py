from datetime import UTC, date, datetime

from agents.tools.filings_search import FilingsSearchArgs, FilingsSearchTool
from models.company import Company
from models.document_chunk import EMBEDDING_DIM, DocumentChunk
from models.enums import FilingType
from models.filing import Filing

NOW = datetime.now(UTC)


class _StubEmbedder:
    model_name = "stub"
    dimension = EMBEDDING_DIM

    def embed(self, texts):
        return [[1.0] + [0.0] * (EMBEDDING_DIM - 1) for _ in texts]


def test_search_filings_scoped_to_ticker_returns_citations(db_session):
    company = Company(ticker="ZZAGT3", name="ZZ Agent Test 3", cik="0009990003")
    db_session.add(company)
    db_session.flush()
    filing = Filing(
        company_id=company.id,
        filing_type=FilingType.FORM_10Q,
        filing_date=date(2025, 12, 18),
        accession_number="TEST-AGT-0001",
        source_url="https://example.com/zzagt3.htm",
        retrieved_at=NOW,
    )
    db_session.add(filing)
    db_session.flush()
    db_session.add(
        DocumentChunk(
            filing_id=filing.id,
            company_id=company.id,
            chunk_index=0,
            section="Item 1A",
            text="A very distinctive sentence about zzagt3 flurbnicated risk exposure.",
            token_count=8,
            embedding=[1.0] + [0.0] * (EMBEDDING_DIM - 1),
            embedding_model="stub",
            retrieved_at=NOW,
        )
    )
    db_session.flush()

    tool = FilingsSearchTool(db_session, _StubEmbedder())
    outcome = tool.run(FilingsSearchArgs(query="flurbnicated risk exposure", ticker="ZZAGT3"))

    assert outcome.success
    assert len(outcome.citations) == 1
    assert outcome.citations[0].ticker == "ZZAGT3"
    assert "flurbnicated" in outcome.data["context_text"]


def test_search_filings_unknown_ticker_returns_empty(db_session):
    tool = FilingsSearchTool(db_session, _StubEmbedder())
    outcome = tool.run(FilingsSearchArgs(query="anything", ticker="NOSUCHTICKER"))

    assert outcome.success
    assert outcome.data["chunks"] == []
    assert outcome.citations == []
