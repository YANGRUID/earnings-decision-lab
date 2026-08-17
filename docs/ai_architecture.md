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
