"""Explicit agent orchestration:

  question -> INTENT CLASSIFICATION -> PLANNING (native tool-calling, or a
  structured-planner fallback for providers without tool-calling support)
  -> TOOL EXECUTION -> EVIDENCE COLLECTION -> SYNTHESIS -> VERIFICATION
  (with one bounded revision attempt) -> final AgentResponse.

This is not a single LLM call — each stage is a separately-testable method,
and the pipeline branches on the configured provider's real capabilities
(``LLMProvider.capabilities.supports_tool_calling``) rather than assuming
every provider behaves identically. See docs/ai_architecture.md.

Failure recovery: intent classification and verification are best-effort —
an LLMError there degrades the pipeline (falls back to "unknown" intent, or
skips verification) rather than failing the whole request. A tool that
raises is caught per-call and recorded as a failed ToolCallRecord; the
synthesis step is told which tools failed so it can answer honestly around
the gap instead of the whole request crashing.
"""

import json
import time

from sqlalchemy.orm import Session

from agents.cost import estimate_cost_usd
from agents.tools.base import Tool
from agents.tools.registry import build_tool_registry
from agents.tools.types import ToolOutcome
from agents.types import AgentResponse, ExecutionTrace, ToolCallRecord
from observability.redact import redact
from prompts.agent_intent import SYSTEM_PROMPT as INTENT_SYSTEM_PROMPT
from prompts.agent_planning import TOOL_CALLING_SYSTEM_PROMPT, build_structured_planner_prompt
from prompts.agent_synthesis import SYSTEM_PROMPT as SYNTHESIS_SYSTEM_PROMPT
from prompts.agent_synthesis import build_synthesis_user_prompt
from prompts.agent_verification import SYSTEM_PROMPT as VERIFICATION_SYSTEM_PROMPT
from prompts.agent_verification import build_verification_user_prompt
from rag.context import Citation
from rag.embeddings import EmbeddingProvider
from schemas.agent import IntentCategory, IntentClassification, ToolPlan, VerificationResult
from services.llm.base import LLMProvider
from services.llm.errors import LLMError
from services.llm.types import ChatMessage, TokenUsage

MAX_TOOL_CALLS = 6


class AgentOrchestrator:
    def __init__(self, db: Session, llm: LLMProvider, embedder: EmbeddingProvider) -> None:
        self._db = db
        self._llm = llm
        self._tools: dict[str, Tool] = build_tool_registry(db, embedder)

    def run(self, question: str) -> AgentResponse:
        start = time.monotonic()
        input_tokens = 0
        output_tokens = 0

        intent = self._safe_classify_intent(question)

        if self._llm.capabilities.supports_tool_calling:
            planning_method = "native_tool_calling"
            tool_results, draft_answer, tokens = self._run_native_tool_calling(question)
        else:
            planning_method = "structured_planner"
            tool_results, draft_answer, tokens = self._run_structured_planner(question)
        input_tokens += tokens[0]
        output_tokens += tokens[1]

        evidence_text, citations = _assemble_evidence(tool_results)
        tool_records = [record for record, _outcome in tool_results]

        verification, revised_answer, verify_tokens = self._verify_and_maybe_revise(
            question, evidence_text, draft_answer
        )
        input_tokens += verify_tokens[0]
        output_tokens += verify_tokens[1]

        total_duration_ms = (time.monotonic() - start) * 1000
        usage = TokenUsage(input_tokens=input_tokens, output_tokens=output_tokens)
        has_usage = input_tokens or output_tokens
        cost = estimate_cost_usd(self._llm.model, usage) if has_usage else None

        trace = ExecutionTrace(
            intent_category=intent.category.value if intent else IntentCategory.GENERAL.value,
            planning_method=planning_method,
            tool_calls=tool_records,
            verification_ran=verification is not None,
            verification_supported=verification.supported if verification else None,
            revised=revised_answer is not None,
            model=self._llm.model,
            total_input_tokens=input_tokens,
            total_output_tokens=output_tokens,
            estimated_cost_usd=cost,
            total_duration_ms=total_duration_ms,
        )
        return AgentResponse(
            question=question,
            answer=revised_answer or draft_answer,
            citations=citations,
            trace=trace,
        )

    # --- Stage 1: intent classification (best-effort) ------------------

    def _safe_classify_intent(self, question: str) -> IntentClassification | None:
        try:
            messages = [
                ChatMessage(role="system", content=INTENT_SYSTEM_PROMPT),
                ChatMessage(role="user", content=question),
            ]
            return self._llm.generate_structured(
                messages, IntentClassification, temperature=0.0, max_tokens=200
            )
        except LLMError:
            return None

    # --- Stage 2a: planning + execution via native tool calling ---------

    def _run_native_tool_calling(
        self, question: str
    ) -> tuple[list[tuple[ToolCallRecord, ToolOutcome | None]], str, tuple[int, int]]:
        messages = [
            ChatMessage(role="system", content=TOOL_CALLING_SYSTEM_PROMPT),
            ChatMessage(role="user", content=question),
        ]
        try:
            result = self._llm.generate(
                messages,
                tools=[t.to_definition() for t in self._tools.values()],
                temperature=0.0,
                max_tokens=1024,
            )
        except LLMError as exc:
            return [], f"The research assistant is temporarily unavailable ({exc}).", (0, 0)

        input_tokens = result.usage.input_tokens if result.usage else 0
        output_tokens = result.usage.output_tokens if result.usage else 0

        if not result.tool_calls:
            return [], result.content or "", (input_tokens, output_tokens)

        tool_results = [
            self._execute_tool(tc.name, tc.arguments) for tc in result.tool_calls[:MAX_TOOL_CALLS]
        ]
        evidence_text, _ = _assemble_evidence(tool_results)
        draft_answer, synth_tokens = self._synthesize(question, evidence_text)
        return (
            tool_results,
            draft_answer,
            (input_tokens + synth_tokens[0], output_tokens + synth_tokens[1]),
        )

    # --- Stage 2b: structured-planner fallback ---------------------------

    def _run_structured_planner(
        self, question: str
    ) -> tuple[list[tuple[ToolCallRecord, ToolOutcome | None]], str, tuple[int, int]]:
        catalog = "\n".join(
            f"- {t.name}: {t.description}\n"
            f"  args schema: {json.dumps(t.args_schema.model_json_schema())}"
            for t in self._tools.values()
        )
        messages = [
            ChatMessage(role="system", content=build_structured_planner_prompt(catalog)),
            ChatMessage(role="user", content=question),
        ]
        try:
            plan: ToolPlan = self._llm.generate_structured(
                messages, ToolPlan, temperature=0.0, max_tokens=800
            )
        except LLMError as exc:
            return [], f"The research assistant is temporarily unavailable ({exc}).", (0, 0)

        tool_results = [
            self._execute_tool(item.tool_name, item.arguments)
            for item in plan.items[:MAX_TOOL_CALLS]
        ]
        evidence_text, _ = _assemble_evidence(tool_results)
        draft_answer, synth_tokens = self._synthesize(question, evidence_text)
        return tool_results, draft_answer, synth_tokens

    # --- Tool execution (shared by both planning paths) ------------------

    def _execute_tool(
        self, name: str, arguments: dict
    ) -> tuple[ToolCallRecord, ToolOutcome | None]:
        start = time.monotonic()
        tool = self._tools.get(name)
        if tool is None:
            duration_ms = (time.monotonic() - start) * 1000
            return (
                ToolCallRecord(
                    tool_name=name,
                    arguments=arguments,
                    success=False,
                    duration_ms=duration_ms,
                    summary="",
                    error=f"unknown tool {name!r}",
                ),
                None,
            )
        try:
            args_obj = tool.args_schema.model_validate(arguments)
            outcome = tool.run(args_obj)
            duration_ms = (time.monotonic() - start) * 1000
            record = ToolCallRecord(
                tool_name=name,
                arguments=arguments,
                success=outcome.success,
                duration_ms=duration_ms,
                summary=outcome.summary,
                error=outcome.error,
                query_description=outcome.query_description,
            )
            return record, outcome
        except Exception as exc:  # noqa: BLE001 — any tool failure must degrade, not crash
            duration_ms = (time.monotonic() - start) * 1000
            return (
                ToolCallRecord(
                    tool_name=name,
                    arguments=arguments,
                    success=False,
                    duration_ms=duration_ms,
                    summary="",
                    error=redact(str(exc)),
                ),
                None,
            )

    # --- Stage 3: synthesis ----------------------------------------------

    def _synthesize(self, question: str, evidence_text: str) -> tuple[str, tuple[int, int]]:
        if not evidence_text.strip():
            return "", (0, 0)
        messages = [
            ChatMessage(role="system", content=SYNTHESIS_SYSTEM_PROMPT),
            ChatMessage(role="user", content=build_synthesis_user_prompt(question, evidence_text)),
        ]
        try:
            result = self._llm.generate(messages, temperature=0.0, max_tokens=900)
        except LLMError as exc:
            return f"The research assistant could not complete this answer ({exc}).", (0, 0)
        usage = (result.usage.input_tokens, result.usage.output_tokens) if result.usage else (0, 0)
        return result.content or "", usage

    # --- Stage 4: verification (best-effort, one bounded revision) -------

    def _verify_and_maybe_revise(
        self, question: str, evidence_text: str, draft_answer: str
    ) -> tuple[VerificationResult | None, str | None, tuple[int, int]]:
        if not evidence_text.strip() or not draft_answer.strip():
            return None, None, (0, 0)
        try:
            messages = [
                ChatMessage(role="system", content=VERIFICATION_SYSTEM_PROMPT),
                ChatMessage(
                    role="user", content=build_verification_user_prompt(evidence_text, draft_answer)
                ),
            ]
            verification = self._llm.generate_structured(
                messages, VerificationResult, temperature=0.0, max_tokens=500
            )
        except LLMError:
            return None, None, (0, 0)

        if verification.supported:
            return verification, None, (0, 0)

        try:
            revise_messages = [
                ChatMessage(role="system", content=SYNTHESIS_SYSTEM_PROMPT),
                ChatMessage(
                    role="user",
                    content=(
                        build_synthesis_user_prompt(question, evidence_text)
                        + "\n\nYour previous draft contained claims not supported by the "
                        f"evidence: {verification.unsupported_claims}. Revise the answer to "
                        "remove or correct them, using only the evidence above."
                    ),
                ),
            ]
            result = self._llm.generate(revise_messages, temperature=0.0, max_tokens=900)
        except LLMError:
            return verification, None, (0, 0)

        usage = (result.usage.input_tokens, result.usage.output_tokens) if result.usage else (0, 0)
        return verification, (result.content or None), usage


def _assemble_evidence(
    tool_results: list[tuple[ToolCallRecord, ToolOutcome | None]],
) -> tuple[str, list[Citation]]:
    """Builds the evidence text handed to synthesis/verification, and the
    final citation list. Filing-search results keep their [N] markers as
    produced by rag.context.assemble_context; if more than one filing-search
    call happens in a single query (uncommon but possible), each is kept in
    its own clearly-labeled block rather than globally renumbered — a
    documented simplification, see docs/ai_architecture.md.
    """
    blocks: list[str] = []
    citations: list[Citation] = []
    for record, outcome in tool_results:
        if outcome is None:
            blocks.append(f"### {record.tool_name} — FAILED\n{record.error}")
            continue
        if not outcome.success:
            blocks.append(f"### {record.tool_name}\n{outcome.error or outcome.summary}")
            continue
        if outcome.citations:
            blocks.append(
                f"### {record.tool_name}\n{outcome.summary}\n{outcome.data.get('context_text', '')}"
            )
            citations.extend(outcome.citations)
        else:
            blocks.append(
                f"### {record.tool_name}\n{outcome.summary}\n"
                f"Data: {json.dumps(outcome.data, default=str)}"
            )
    return "\n\n".join(blocks), citations
