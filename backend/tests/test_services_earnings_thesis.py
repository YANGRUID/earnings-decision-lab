from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal

from pydantic import BaseModel

from models.company import Company
from models.document_chunk import EMBEDDING_DIM
from models.earnings_event import EarningsEvent
from models.earnings_result import EarningsResult
from models.price_reaction import PriceReaction
from rag.embeddings import EmbeddingProvider
from schemas.thesis import EarningsThesis
from services.earnings_thesis import (
    ThesisGenerationError,
    generate_earnings_thesis,
)
from services.llm.base import LLMProvider
from services.llm.errors import LLMError
from services.llm.types import Capabilities, GenerateResult

CLEAN_THESIS_KWARGS = {
    "business_context": "The filings describe a semiconductor business [1].",
    "historical_earnings_pattern": "Past reports show mixed results per the evidence.",
    "guidance_trend": "No guidance comparison was available.",
    "key_risks": "Real risk factors from the filings [1].",
    "market_setup": "The real implied move and estimate are described above.",
    "disclaimer": "This is not investment advice and no outcome is assured.",
}


def _clean_thesis() -> EarningsThesis:
    return EarningsThesis(**CLEAN_THESIS_KWARGS)


def _bad_thesis() -> EarningsThesis:
    kwargs = dict(CLEAN_THESIS_KWARGS)
    kwargs["disclaimer"] = "This strategy is guaranteed to profit with no risk."
    return EarningsThesis(**kwargs)


class _StubEmbedder(EmbeddingProvider):
    model_name = "stub"
    dimension = EMBEDDING_DIM

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0] + [0.0] * (EMBEDDING_DIM - 1) for _ in texts]


class _StubLLM(LLMProvider):
    name = "stub"
    model = "stub-model"
    capabilities = Capabilities(
        supports_structured_output=True, supports_tool_calling=False, supports_streaming=False
    )

    def __init__(self, responses: list[EarningsThesis] | None = None, error: bool = False) -> None:
        self._responses = list(responses) if responses is not None else [_clean_thesis()]
        self._error = error
        self.calls = 0

    def generate(self, messages, *, tools=None, temperature=0.0, max_tokens=1024):
        return GenerateResult(content="unused")

    def generate_structured(
        self, messages, schema: type[BaseModel], *, temperature=0.0, max_tokens=1024
    ):
        self.calls += 1
        if self._error:
            raise LLMError("stub failure")
        if schema is not EarningsThesis:
            raise NotImplementedError(schema)
        index = min(self.calls - 1, len(self._responses) - 1)
        return self._responses[index]

    def stream(self, messages, *, temperature=0.0, max_tokens=1024) -> Iterator[str]:
        raise NotImplementedError


def _seed_company(db_session, ticker: str = "ZZTHES") -> Company:
    company = Company(ticker=ticker, name="ZZ Thesis Co", cik="0009999940")
    db_session.add(company)
    db_session.flush()
    return company


def _seed_earnings_history(db_session, company: Company) -> None:
    event = EarningsEvent(
        company_id=company.id, fiscal_year=2026, fiscal_quarter=2, earnings_date=date(2026, 5, 20)
    )
    db_session.add(event)
    db_session.flush()
    db_session.add(
        EarningsResult(
            earnings_event_id=event.id,
            actual_eps=Decimal("1.23"),
            source_provider="test",
            retrieved_at=datetime.now(UTC),
        )
    )
    db_session.add(
        PriceReaction(
            earnings_event_id=event.id,
            next_day_move_pct=Decimal("-0.05"),
            source_provider="test",
            retrieved_at=datetime.now(UTC),
        )
    )
    db_session.flush()


def test_generates_thesis_from_real_evidence(db_session):
    company = _seed_company(db_session)
    _seed_earnings_history(db_session, company)
    llm = _StubLLM()

    result = generate_earnings_thesis(db_session, llm, _StubEmbedder(), company)

    assert result.thesis.business_context == CLEAN_THESIS_KWARGS["business_context"]
    assert result.model == "stub-model"
    assert llm.calls == 1  # no prohibited phrases -> no retry needed


def test_missing_evidence_does_not_crash_and_still_produces_a_result(db_session):
    company = _seed_company(db_session)  # no earnings, no filings, no options data at all
    llm = _StubLLM()

    result = generate_earnings_thesis(db_session, llm, _StubEmbedder(), company)

    assert result.thesis is not None
    assert result.citations == []


def test_prohibited_phrase_triggers_one_retry_and_succeeds(db_session):
    company = _seed_company(db_session)
    llm = _StubLLM(responses=[_bad_thesis(), _clean_thesis()])

    result = generate_earnings_thesis(db_session, llm, _StubEmbedder(), company)

    assert llm.calls == 2
    assert "guaranteed" not in result.thesis.disclaimer.lower()


def test_prohibited_phrase_still_present_after_retry_raises(db_session):
    company = _seed_company(db_session)
    llm = _StubLLM(responses=[_bad_thesis(), _bad_thesis()])

    try:
        generate_earnings_thesis(db_session, llm, _StubEmbedder(), company)
        raise AssertionError("expected ThesisGenerationError")
    except ThesisGenerationError as exc:
        assert "guaranteed" in str(exc)
    assert llm.calls == 2  # never retried a third time


def test_llm_error_raises_thesis_generation_error(db_session):
    company = _seed_company(db_session)
    llm = _StubLLM(error=True)

    try:
        generate_earnings_thesis(db_session, llm, _StubEmbedder(), company)
        raise AssertionError("expected ThesisGenerationError")
    except ThesisGenerationError:
        pass
