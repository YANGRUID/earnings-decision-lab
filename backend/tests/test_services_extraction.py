from collections.abc import Iterator
from datetime import UTC, date, datetime

from pydantic import BaseModel

from models.ai_extraction import AIExtraction
from models.company import Company
from models.document_chunk import EMBEDDING_DIM, DocumentChunk
from models.enums import FilingType
from models.filing import Filing
from schemas.extraction import GuidanceComparisonThemes, GuidanceExtraction, RevenueGuidance
from services.extraction import compare_commentary_themes, extract_guidance
from services.llm.base import LLMProvider
from services.llm.types import Capabilities

NOW = datetime.now(UTC)


class _StubStructuredLLM(LLMProvider):
    name = "stub"
    model = "stub-model-v1"
    capabilities = Capabilities(
        supports_structured_output=True, supports_tool_calling=False, supports_streaming=False
    )

    def __init__(self, canned_result: BaseModel) -> None:
        self._canned_result = canned_result
        self.last_messages = None
        self.last_schema = None

    def generate(self, messages, *, tools=None, temperature=0.0, max_tokens=1024):
        raise NotImplementedError

    def generate_structured(
        self, messages, schema: type[BaseModel], *, temperature=0.0, max_tokens=1024
    ):
        self.last_messages = messages
        self.last_schema = schema
        return self._canned_result

    def stream(self, messages, *, temperature=0.0, max_tokens=1024) -> Iterator[str]:
        raise NotImplementedError


def _seed_filing_and_chunk(db_session) -> tuple[Filing, DocumentChunk]:
    company = Company(ticker="ZZTEST9", name="ZZ9 Inc", cik="0009999997")
    db_session.add(company)
    db_session.flush()
    filing = Filing(
        company_id=company.id,
        filing_type=FilingType.FORM_10Q,
        filing_date=date(2025, 12, 18),
        accession_number="TEST-0000000199",
        source_url="https://example.com/zz9.htm",
        retrieved_at=NOW,
    )
    db_session.add(filing)
    db_session.flush()
    chunk = DocumentChunk(
        filing_id=filing.id,
        company_id=company.id,
        chunk_index=0,
        section="Item 7",
        text="We expect revenue between $100M and $120M next quarter.",
        token_count=10,
        embedding=[0.0] * EMBEDDING_DIM,
        embedding_model="test",
        retrieved_at=NOW,
    )
    db_session.add(chunk)
    db_session.flush()
    return filing, chunk


def test_extract_guidance_persists_row_with_provenance(db_session):
    filing, chunk = _seed_filing_and_chunk(db_session)
    canned = GuidanceExtraction(revenue=RevenueGuidance(low=100, high=120, period="Q1 FY2027"))
    llm = _StubStructuredLLM(canned)

    row = extract_guidance(db_session, llm, filing.id, filing.company_id, [chunk])

    assert row.id is not None
    assert row.filing_id == filing.id
    assert row.extraction_type == "guidance"
    assert row.model == "stub-model-v1"
    assert row.prompt_version == "guidance-extraction-v1"
    assert row.source_chunk_ids == [chunk.id]
    assert row.extracted_data["revenue"]["low"] == "100"

    # actually persisted, not just returned in memory
    reloaded = db_session.get(AIExtraction, row.id)
    assert reloaded is not None
    assert reloaded.extracted_data["revenue"]["high"] == "120"


def test_extract_guidance_sends_chunk_text_to_llm(db_session):
    filing, chunk = _seed_filing_and_chunk(db_session)
    llm = _StubStructuredLLM(GuidanceExtraction())

    extract_guidance(db_session, llm, filing.id, filing.company_id, [chunk])

    user_message = next(m for m in llm.last_messages if m.role == "user")
    assert "$100M and $120M" in user_message.content
    assert llm.last_schema is GuidanceExtraction


def test_compare_commentary_themes_uses_prior_and_current_lists():
    previous = GuidanceExtraction(key_drivers=["gaming demand"], risks=["supply constraints"])
    current = GuidanceExtraction(key_drivers=["AI server demand"], risks=["supply constraints"])
    canned = GuidanceComparisonThemes(
        new_positive_themes=["AI server demand"],
        removed_themes=["gaming demand"],
    )
    llm = _StubStructuredLLM(canned)

    result = compare_commentary_themes(llm, previous, current)

    assert result.new_positive_themes == ["AI server demand"]
    assert result.removed_themes == ["gaming demand"]
    user_message = next(m for m in llm.last_messages if m.role == "user")
    assert "gaming demand" in user_message.content
    assert "AI server demand" in user_message.content
    assert llm.last_schema is GuidanceComparisonThemes
