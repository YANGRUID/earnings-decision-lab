from collections.abc import Iterator
from datetime import UTC, date, datetime

from pydantic import BaseModel

from models.company import Company
from models.document_chunk import EMBEDDING_DIM, DocumentChunk
from models.enums import FilingType
from models.filing import Filing
from rag.answer import answer_question
from rag.embeddings import EmbeddingProvider
from rag.retrieval import RetrievalFilters
from services.llm.base import LLMProvider
from services.llm.types import Capabilities, GenerateResult

NOW = datetime.now(UTC)


class _StubEmbeddingProvider(EmbeddingProvider):
    model_name = "stub"
    dimension = EMBEDDING_DIM

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0] + [0.0] * (EMBEDDING_DIM - 1) for _ in texts]


class _StubLLMProvider(LLMProvider):
    name = "stub"
    capabilities = Capabilities(
        supports_structured_output=True, supports_tool_calling=True, supports_streaming=False
    )

    def __init__(self, canned_content: str) -> None:
        self._canned_content = canned_content
        self.last_messages = None

    def generate(self, messages, *, tools=None, temperature=0.0, max_tokens=1024):
        self.last_messages = messages
        return GenerateResult(content=self._canned_content, finish_reason="stop")

    def generate_structured(
        self, messages, schema: type[BaseModel], *, temperature=0.0, max_tokens=1024
    ):
        raise NotImplementedError

    def stream(self, messages, *, temperature=0.0, max_tokens=1024) -> Iterator[str]:
        raise NotImplementedError


def test_answer_question_grounds_and_cites(db_session):
    company = Company(ticker="ZZTEST7", name="MU3 Inc", cik="0009999999")
    db_session.add(company)
    db_session.flush()
    filing = Filing(
        company_id=company.id,
        filing_type=FilingType.FORM_10Q,
        filing_date=date(2025, 12, 18),
        accession_number="TEST-0000000099",
        source_url="https://example.com/test.htm",
        retrieved_at=NOW,
    )
    db_session.add(filing)
    db_session.flush()
    db_session.add(
        DocumentChunk(
            filing_id=filing.id,
            company_id=company.id,
            chunk_index=0,
            section="Item 7",
            text="Gross margin improved due to favorable pricing.",
            token_count=6,
            embedding=[1.0] + [0.0] * (EMBEDDING_DIM - 1),
            embedding_model="stub",
            retrieved_at=NOW,
        )
    )
    db_session.flush()

    llm = _StubLLMProvider("Gross margin improved [1].")
    # Scoped to this test's own company: the real, permanently-seeded corpus
    # discusses gross margin extensively too, so an unscoped search would
    # not deterministically return only this fixture chunk.
    result = answer_question(
        db_session,
        llm,
        _StubEmbeddingProvider(),
        "What happened to gross margin?",
        filters=RetrievalFilters(company_ids=[company.id]),
    )

    assert result.answer == "Gross margin improved [1]."
    assert result.retrieved_chunk_count == 1
    assert result.citations[0].ticker == "ZZTEST7"
    # the LLM must have actually been given the retrieved context, not just the question
    user_message = next(m for m in llm.last_messages if m.role == "user")
    assert "Gross margin improved due to favorable pricing." in user_message.content


def test_answer_question_no_matches_skips_llm_call(db_session):
    # vector_search has no relevance floor (it always returns the k nearest
    # neighbors, however distant), so "no matches" is only guaranteed by
    # scoping to a company with zero chunks — not by picking an
    # off-topic-sounding query against the whole (now real-data) corpus.
    empty_company = Company(ticker="ZZTEST8", name="Empty Co", cik="0009999998")
    db_session.add(empty_company)
    db_session.flush()

    llm = _StubLLMProvider("should not be used")
    result = answer_question(
        db_session,
        llm,
        _StubEmbeddingProvider(),
        "anything at all",
        filters=RetrievalFilters(company_ids=[empty_company.id]),
    )

    assert result.retrieved_chunk_count == 0
    assert result.citations == []
    assert llm.last_messages is None
