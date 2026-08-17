"""API response schemas. Deliberately separate from internal ORM models and
from the extraction/agent schemas — the wire contract is allowed to differ
from internal representations without those changing in lockstep.
"""

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from rag.context import Citation


class CompanyResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    ticker: str
    name: str
    sector: str | None
    exchange: str | None


class EarningsResultResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    actual_eps: Decimal | None
    actual_revenue: Decimal | None
    gross_margin: Decimal | None
    reported_at: datetime | None
    source_provider: str


class PriceReactionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    close_price_before: Decimal | None
    next_day_close: Decimal | None
    five_day_close: Decimal | None
    next_day_move_pct: Decimal | None
    five_day_move_pct: Decimal | None


class EarningsEventSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    fiscal_year: int
    fiscal_quarter: int
    earnings_date: date | None
    date_confirmed: bool


class EarningsEventDetail(EarningsEventSummary):
    company: CompanyResponse
    result: EarningsResultResponse | None
    price_reaction: PriceReactionResponse | None


class CitationResponse(BaseModel):
    marker: str
    ticker: str
    filing_type: str
    filing_date: date
    section: str | None
    source_url: str

    @classmethod
    def from_citation(cls, citation: Citation) -> "CitationResponse":
        return cls(
            marker=citation.marker,
            ticker=citation.ticker,
            filing_type=citation.filing_type,
            filing_date=citation.filing_date,
            section=citation.section,
            source_url=citation.source_url,
        )


class FilingSearchResponse(BaseModel):
    context_text: str
    citations: list[CitationResponse]


class ToolCallResponse(BaseModel):
    tool_name: str
    arguments: dict
    success: bool
    duration_ms: float
    summary: str
    error: str | None
    query_description: str | None


class ExecutionTraceResponse(BaseModel):
    intent_category: str
    planning_method: str
    tool_calls: list[ToolCallResponse]
    verification_ran: bool
    verification_supported: bool | None
    revised: bool
    model: str
    total_input_tokens: int
    total_output_tokens: int
    estimated_cost_usd: Decimal | None
    total_duration_ms: float


class ResearchQueryRequest(BaseModel):
    question: str


class ResearchQueryResponse(BaseModel):
    question: str
    answer: str
    citations: list[CitationResponse]
    trace: ExecutionTraceResponse
