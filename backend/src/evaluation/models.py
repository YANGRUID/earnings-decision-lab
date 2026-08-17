"""Typed shapes for evaluation datasets (read by the runner scripts in
``evaluation/scripts/``) and evaluation results (written to
``evaluation/results/*.json`` and served by ``GET /api/v1/evaluations``).

Kept in the backend package, not the top-level ``evaluation/`` directory,
so both the runner scripts and the FastAPI app import the same schema
instead of two copies drifting apart.
"""

from datetime import datetime

from pydantic import BaseModel


class RetrievalQAItem(BaseModel):
    id: str
    query: str
    ticker: str
    relevant_chunk_ids: list[int]
    note: str


class RagAnswerQAItem(BaseModel):
    id: str
    query: str
    ticker: str
    relevant_chunk_ids: list[int]
    required_facts: list[str]
    note: str


class AgentQAItem(BaseModel):
    id: str
    query: str
    expected_intent: str
    expected_tools: list[str]
    note: str


class ExtractionGroundTruthItem(BaseModel):
    id: str
    ticker: str
    filing_id: int
    company_id: int
    chunk_id_start: int
    chunk_id_end: int
    expected_capex_low: float | None
    expected_capex_high: float | None
    acceptable_tones: list[str]
    expected_keyword_hits: list[str]
    note: str


class RetrievalItemResult(BaseModel):
    id: str
    query: str
    recall_at_3: float
    recall_at_5: float
    recall_at_10: float
    mrr: float
    retrieved_count: int


class RagAnswerItemResult(BaseModel):
    id: str
    query: str
    fact_coverage: float
    missing_facts: list[str]
    citation_precision: float
    citation_completeness: float
    retrieved_chunk_count: int
    answer_had_no_evidence: bool
    duration_ms: float


class AgentItemResult(BaseModel):
    id: str
    query: str
    expected_intent: str
    actual_intent: str
    intent_correct: bool
    expected_tools: list[str]
    actual_tools: list[str]
    tools_correct: bool
    verification_ran: bool
    verification_supported: bool | None
    total_duration_ms: float
    total_input_tokens: int
    total_output_tokens: int
    estimated_cost_usd: float | None


class ExtractionItemResult(BaseModel):
    id: str
    ticker: str
    non_capex_fields_correctly_null: bool
    capex_correct: bool
    extracted_capex_low: float | None
    extracted_capex_high: float | None
    tone: str | None
    tone_plausible: bool
    key_driver_keyword_hit: bool


class RetrievalSummary(BaseModel):
    item_count: int
    mean_recall_at_3: float
    mean_recall_at_5: float
    mean_recall_at_10: float
    mean_mrr: float
    items: list[RetrievalItemResult]


class RagAnswerSummary(BaseModel):
    item_count: int
    mean_fact_coverage: float
    fully_correct_count: int
    mean_citation_precision: float
    mean_citation_completeness: float
    mean_duration_ms: float
    items: list[RagAnswerItemResult]


class AgentSummary(BaseModel):
    item_count: int
    intent_accuracy: float
    tool_selection_accuracy: float
    verification_run_rate: float
    mean_duration_ms: float
    total_estimated_cost_usd: float
    items: list[AgentItemResult]


class ExtractionSummary(BaseModel):
    item_count: int
    non_capex_null_accuracy: float
    capex_accuracy: float
    tone_plausibility_rate: float
    keyword_hit_rate: float
    items: list[ExtractionItemResult]


class EvaluationRun(BaseModel):
    run_at: datetime
    llm_provider: str
    llm_model: str
    embedding_model: str
    retrieval: RetrievalSummary | None = None
    rag_answer: RagAnswerSummary | None = None
    agent: AgentSummary | None = None
    extraction: ExtractionSummary | None = None
