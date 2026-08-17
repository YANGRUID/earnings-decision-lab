# AI architecture

Covers the RAG pipeline built in Phase 5 (`backend/src/rag/`) and the LLM layer it sits on
(`backend/src/services/llm/`, documented separately in
[llm_providers.md](llm_providers.md)). Structured extraction (Phase 6) and agent orchestration
(Phase 7) build on this — this document is updated as they land.

## Why not "upload PDF → embed → top 5 chunks → GPT"

The explicit anti-pattern this project avoids: a single flat similarity search with no
metadata filtering, no keyword signal, no citation structure, and no separation between
retrieval and synthesis. Instead:

```
SEC EDGAR (real filings)
   │
   ▼
download (raw HTML)                    providers.sec_edgar.get_filing_html
   │
   ▼
parse (BeautifulSoup + Item-heading     rag.parsing.html_to_text /
       detection)                       split_into_sections
   │
   ▼
chunk (section-bounded, token-          rag.chunking.chunk_sections
       approximate, overlapping)
   │
   ▼
embed (local ONNX model, no API key)    rag.embeddings.FastEmbedProvider
   │
   ▼
persist (pgvector + full-text index)    models.DocumentChunk
   │
   ▼
retrieve: vector search + keyword       rag.retrieval.{vector_search,
   search, fused by RRF                  keyword_search, hybrid_search}
   │
   ▼
assemble context + generate citations   rag.context.assemble_context
   │
   ▼
synthesize grounded, cited answer       rag.answer.answer_question
   (provider-agnostic LLM call)
```

Every stage is a separate, independently unit-tested module. Metadata filtering
(`RetrievalFilters`: company, filing type, date range) is a first-class part of retrieval, not
an afterthought — a query can be scoped to "MU's last four 10-Qs" before either search even
runs.

## Real data, run end to end

As of this phase: **2,231 real chunks from 93 real SEC filings** (10-K/10-Q/8-K) across
NVDA/AMD/MU/SNDK, embedded and indexed. Example live query, unedited:

> **Q:** What did Micron say about HBM demand in its most recent risk factors?
>
> **A:** Based on the provided context, Micron's most recent risk factors (from the 10-Q filed
> 2026-06-25) state that demand for HBM and other advanced products has increased due to
> generative AI models, but the long-term trajectory is unknown and associated demand may
> fluctuate [3]. Additionally, if HBM demand weakens and suppliers shift capacity to
> conventional DRAM, this could lead to an oversupplied DRAM market and downward pressure on
> pricing... [3]
>
> **Citations:** [1] MU 10-K 2025-10-03, Item 1A · [2] MU 10-Q 2025-12-18, Item 1A ·
> [3] MU 10-Q 2026-06-25, Item 1A · [4] MU 10-K 2025-10-03, Item 7

Retrieved via hybrid search, synthesized by DeepSeek through the provider-agnostic LLM layer,
citations generated from the actual retrieved chunks (not parsed back out of the model's free
text).

## Parsing — real, with a stated limit

Phase 1 used a naive regex tag-stripper as a one-off provenance check. Phase 5 replaces it for
actual RAG use with real DOM parsing (BeautifulSoup) plus "Item N." heading detection
(`rag/parsing.py`). Section detection is regex pattern matching on cleaned text, not a
structural parse of SEC's own semantic markup — real filings are inconsistent enough across
companies, years, and HTML generators that a fully structural parser is a much larger
undertaking than this project's scope justifies. This means section boundaries are
best-effort, not guaranteed exact on every filing (see [limitations.md](limitations.md)).

## Chunking — token-approximate, section-bounded

`rag/chunking.py` never lets a chunk span two sections, so every chunk's section label is
unambiguous for citation. "Token count" is a whitespace-word count, not exact tokenization for
any specific vendor — deliberate, since this project is provider-agnostic (DeepSeek/OpenAI/
Anthropic all tokenize differently) and exact per-request token accounting belongs at the point
of an actual LLM call, not baked into chunk sizing.

## Embeddings — local, no API key, and why

No configured LLM provider currently offers embeddings: DeepSeek's official pricing/models page
lists only its two chat models (verified live, no embeddings endpoint); `OPENAI_API_KEY` isn't
configured; Anthropic doesn't offer embeddings directly (their own docs point to Voyage AI, a
separate vendor requiring another account this project can't create autonomously). `fastembed`
(ONNX-based, `BAAI/bge-small-en-v1.5`, 384-dim, ~67MB) needs no key, runs offline, and has a much
smaller footprint than a PyTorch-based alternative like `sentence-transformers` — a real
constraint on the development machine (96% disk full at the time this decision was made). See
`rag/embeddings.py`'s module docstring and [engineering_decisions.md](engineering_decisions.md).

`EmbeddingProvider` is a one-method ABC for the same reason `services.llm` is abstracted —
swapping to a hosted embedding API later (if quality or scale ever demands it) means one new
adapter class, not a rewrite of retrieval.

## Hybrid retrieval — vector + keyword, fused by RRF

`rag/retrieval.py` runs pgvector cosine similarity and PostgreSQL full-text search
independently, then combines them by Reciprocal Rank Fusion:
`score(chunk) = Σ 1 / (rrf_k + rank + 1)` across whichever ranked list(s) it appears in. RRF was
chosen because it needs no score normalization between two signals that aren't on the same
scale (cosine similarity vs. `ts_rank`) — a chunk found by both searches outranks one found by
only one, without pretending the raw scores are comparable.

**No separate reranking model (e.g. a cross-encoder) is used.** At this project's current
scale — four tickers, a few thousand chunks — the added model dependency and latency aren't
demonstrably justified over RRF fusion of two independently-reasonable rankings. This is a
scale-appropriate decision, not a permanent one: swapping in a real reranker later would extend
`hybrid_search`'s output, not replace its interface.

## Context assembly and citations

`rag/context.py` numbers retrieved chunks `[1]`, `[2]`, ... and builds both the prompt context
(with ticker/filing-type/date/section headers per chunk) and a structured `Citation` list. The
citation list a UI would render comes from the actual retrieved chunks, not from parsing the
model's free-text response — a citation always points at something that was genuinely
retrieved, never at something the model merely claimed.

## Synthesis

`rag/answer.py::answer_question` is the full pipeline's proof of correctness, not the final
answer-generation surface for this project — Phase 7's agent orchestration will call the same
retrieval/context modules alongside the deterministic analytics tools (options payoffs,
IV crush, etc.) from Phases 3-4. If no chunks are retrieved, the LLM is not called at all — the
function returns an explicit "no matching content" result rather than letting the model guess
from its own training data, which would defeat the entire point of grounding.

## Structured extraction (Phase 6)

`schemas/extraction.py` defines `GuidanceExtraction` (revenue/EPS/gross-margin/capex guidance,
key drivers, risks, management tone, important topics) and `GuidanceComparisonThemes`
(new/removed commentary themes). `services/extraction.py` retrieves a filing's MD&A/risk-factor
chunks, calls `LLMProvider.generate_structured`, and persists an `AIExtraction` row recording
the source chunk IDs, model, and prompt version (`prompts/guidance_extraction.py`,
versioned as `guidance-extraction-v1`) — full provenance for every extracted field.

**Numeric comparison and textual comparison are two separate code paths, deliberately never
merged**: `analytics/earnings/guidance_comparison.py` computes midpoint changes by exact
arithmetic on two already-extracted ranges — no LLM call. `services/extraction.py::
compare_commentary_themes` is a *separate* LLM call, with its own versioned prompt
(`guidance-comparison-v1`), that judges which commentary *themes* are new/removed — something
genuinely requiring semantic judgment that arithmetic can't do. Neither function calls the
other; a numeric midpoint change is always exact, never a model's paraphrase.

## Agent orchestration (Phase 7)

`agents/orchestrator.py::AgentOrchestrator` — an explicit multi-stage pipeline, not a single LLM
call:

```
question
   │
   ▼
INTENT CLASSIFICATION      generate_structured(IntentClassification)  — best-effort
   │
   ▼
PLANNING                   two real, different code paths depending on
   │                       provider.capabilities.supports_tool_calling:
   │                         • native: generate(tools=[...]) — the LLM chooses
   │                           which real tools to call and with what arguments
   │                         • fallback: generate_structured(ToolPlan) — an explicit
   │                           plan, for a provider without native tool-calling
   ▼
TOOL EXECUTION              each requested call is arg-validated (Pydantic) and run;
   │                        exceptions are caught per-tool, never crash the request
   ▼
EVIDENCE COLLECTION         tool outputs assembled into one evidence block;
   │                        citations from any search_filings calls preserved
   ▼
SYNTHESIS                   generate() — grounded answer using only the evidence
   │
   ▼
VERIFICATION                generate_structured(VerificationResult) — a separate
   │                        LLM call checks the draft against the evidence;
   │                        unsupported claims trigger one bounded revision attempt
   ▼
final AgentResponse (answer, citations, full execution trace)
```

**Seven real tools**, each wrapping already-built, already-tested functionality — the agent
layer adds orchestration, it doesn't reimplement anything:

| Tool | Wraps | Data status |
|---|---|---|
| `get_historical_earnings` | real DB (EarningsEvent/EarningsResult/PriceReaction) | real |
| `search_filings` | Phase 5 hybrid RAG | real (2,231 chunks) |
| `compare_guidance` | Phase 6 extraction + deterministic comparison | real (persisted extractions) |
| `calculate_strategy_payoff` | Phase 3 payoff engine | pure calc, always works |
| `calculate_implied_move` | Phase 3 implied-move methodology | pure calc from supplied quotes |
| `get_options_snapshot` | real `options_snapshot` table | honestly empty (no provider) |
| `run_strategy_replay` | real `strategy_replay` table | honestly empty (no historical chain data) |

The last two tools are a deliberate demonstration of this project's stance on missing data:
they query the real table and report honestly that nothing is there, rather than the
orchestrator silently omitting the capability or a tool fabricating a plausible-looking answer.

**Provider capability handling is real, not cosmetic.** All three currently-implemented
providers (DeepSeek, OpenAI, Anthropic) declare `supports_tool_calling=True`, so the native
path is what actually runs today — but the structured-planner fallback is a genuinely
different code path (a distinct prompt, a distinct schema, a distinct execution branch),
exercised by dedicated tests with a scripted provider that declares
`supports_tool_calling=False`, not just asserted to exist.

**Verification is a real, separate LLM call** — not a rephrasing of the synthesis prompt. It's
given the evidence and the draft answer and asked specifically whether every claim is
supported; a failed verification triggers exactly one bounded revision attempt (never an
unbounded retry loop) with the flagged unsupported claims fed back into a fresh synthesis call.

**Execution traces** record: intent category, planning method used, every tool call (name,
arguments, success/failure, duration, and — for DB tools — the compiled SQL query, safe to
show), verification outcome, whether a revision happened, the model used, real token counts
(from `generate()` calls only — see the known limitation below), and an estimated USD cost from
a small, dated pricing table. No chain-of-thought is ever exposed — only these structured,
already-final artifacts.

**A known, honest limitation:** `LLMProvider.generate_structured` doesn't return token-usage
metadata (only `generate()` does), so intent-classification and verification calls' token cost
is not included in the trace's `total_input_tokens`/`total_output_tokens`/`estimated_cost_usd`.
This understates the true cost by a bounded, usually-small amount (those prompts are much
shorter than the synthesis call). Documented here and in
[engineering_decisions.md](engineering_decisions.md) rather than papered over with a fabricated
estimate.

**Real run, unedited** (native tool-calling, live DeepSeek): *"What were MU's last two earnings
results, and what did they say about HBM demand in their filings?"* — correctly planned two
tool calls (`get_historical_earnings`, `search_filings`), executed both against real data,
synthesized a cited answer (5 citations, all real retrieved sections), verification confirmed
it was fully supported by the evidence with no revision needed. Full trace: intent
`earnings_history`, 2 tool calls (33ms and 20ms), 5,035 input / 796 output tokens, estimated
cost $0.0033, 8.5s total.

**Real run, unedited** (no-tool-needed path): *"Hi, what can you help me with?"* — correctly
classified as `general`, zero tool calls, direct conversational answer, verification correctly
skipped (nothing to verify against).

**Real run, unedited** (structured extraction, Phase 6): extracted guidance from MU's two most
recent 10-Q MD&A sections (2026-03-19 and 2026-06-25) via DeepSeek.
`revenue`/`eps`/`gross_margin`/`capex` came back
`null` for both — a genuine, informative result, not a bug: 10-Q MD&A sections discuss
*historical* results and qualitative commentary, not forward-looking numeric ranges in a
directly extractable format (real forward guidance for these companies typically appears in the
earnings press release or call, not deeply embedded MD&A prose). The schema returned `null`
rather than inventing plausible-looking numbers, exactly as designed. The qualitative fields
worked correctly: real key drivers ("AI-driven memory and storage growth", "strategic customer
agreements"), risks, and tone were extracted from each quarter, and the LLM thematic comparison
correctly identified "strategic customer agreements" as a new theme and eight prior-quarter
themes (including "DRAM and NAND pricing increases") as absent from the current quarter's
retrieved chunks.
