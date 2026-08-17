# Interview walkthrough

Talking points for discussing this project, organized around the questions it's actually
likely to draw — not a restatement of [engineering_decisions.md](engineering_decisions.md)
(which has the full "why" for every decision), but a shorter path through it for a
conversation. Every number here is copied from a real, reproducible source
(`docs/evaluation.md`, `git log`, a real test run) at the time this was written — not recalled
from memory.

## 30-second pitch

A research tool for earnings-event analysis on four semiconductor tickers (NVDA, AMD, MU,
SNDK) that combines deterministic Python financial analytics (options payoffs, implied move,
IV crush — never delegated to an LLM) with an AI research layer: hybrid RAG over real SEC
filings, structured extraction, and a tool-calling agent that plans, executes real tools,
collects evidence, and verifies its own answer before returning it. Built to demonstrate the
full lifecycle — schema design, provider integration, deterministic financial math, RAG,
agent orchestration, a typed API, a real evaluation framework, Docker, and CI — end to end,
not just the AI layer in isolation.

## "Walk me through the architecture"

Five modules, built bottom-up: point-in-time data model → deterministic analytics → typed API
→ AI layer on top, in that order, deliberately — the AI layer calls *into* working analytics
and real data, not the other way around. The data model's one non-negotiable rule is
no-lookahead-bias: every pre-earnings snapshot stores only what existed at that timestamp, and
every externally-sourced record carries its provider and retrieval time, so any join can be
audited for leakage.

The AI layer is genuinely agentic, not a single prompt: intent classification → planning
(native tool-calling, with a structured-planner fallback for providers that don't support it)
→ tool execution against real deterministic tools and real retrieval → evidence collection →
synthesis → a separate verification call → one bounded revision attempt. Seven real tools —
earnings history, filing search, guidance comparison, options payoff, implied move, and two
that honestly report "no data" rather than fabricate (options snapshot, strategy replay,
because no options-chain data provider is wired up — a real, documented gap, not a bug).

## "What was the hardest part?"

Three real stories, not a generic answer:

1. **A credential leak found by testing, not by review.** Building structured latency logging
   in Phase 10 meant turning on root-level logging — which also turned on httpx's own built-in
   request logging, and this project's Tiingo/Alpha Vantage adapters authenticate via a
   `token`/`apikey` query parameter. A live verification call printed a real API key straight
   into what looked like a clean structured log line. Fixed at the root and defensively at
   every other place a raw exception could reach a log line or API response. The lesson that
   stuck: logging infrastructure is attack surface too, and the only way to catch this class of
   bug is to actually run the code with real logging on and read the output, not just review
   the diff.
2. **A packaging bug that only surfaced on the first real Docker build.** The wheel's
   `[tool.hatch.build.targets.wheel]` config had been wrong since Phase 0 — it shipped
   `src/agents/...` instead of `agents/...` — but nothing caught it for ten phases because the
   test suite imports via pytest's `pythonpath`, not an installed package. The first time
   anything actually tried to `uv build` and run the result (Phase 10's Dockerfile), every
   import failed. Confirmed the root cause by inspecting the actual wheel contents before
   guessing at a fix, then checked current Hatch docs for the correct multi-package
   configuration rather than trial-and-error. The lesson: a test suite that never exercises the
   actual build artifact has a real blind spot, and "the tests pass" isn't the same claim as
   "the built package works."
3. **Retrieval quality was worse than assumed, and the evaluation framework is what proved
   it.** Recall@5 came in at 35% — a real, unflattering number. Investigating *why* (not just
   reporting the score) found a specific, explainable cause: boilerplate section-opener text
   and repeated financial-statement tables score competitively against the actual narrative
   sentence that answers a question, for both vector and keyword search. Also found — and this
   mattered for trusting the rest of the framework — that the evaluation dataset itself had two
   real labeling mistakes (missed capex guidance in Micron's filings; retrieval items missing a
   legitimately duplicate source chunk), caught and fixed during construction rather than
   shipped. Both are called out by name in `docs/evaluation.md`, not smoothed over.

## "What would you do differently, or what's missing?"

Answered honestly, not defensively — `docs/limitations.md` is the canonical, always-current
list, organized by phase:

- No live options-chain data provider is wired up (every free option evaluated either lacks
  historical coverage or requires a paid subscription — documented in `docs/data_sources.md`),
  so implied move / IV / historical strategy replay are architecturally complete but run on
  honestly-empty tables. This is the single biggest feature gap.
- The agent does single-round tool-calling, not a full multi-turn ReAct loop — a real, stated
  scope boundary, not an oversight.
- No LLM-as-judge secondary evaluation signal (the deterministic fact-coverage check has a
  known false-negative risk on paraphrased-but-correct answers — documented, not hidden).
- No live cloud deployment — `docker compose up --build` runs the full real stack locally and
  is CI-verified on every push, but going live means real recurring personal cost and cloud
  credentials; see `docs/deployment.md` for the researched cost comparison and why that's a
  deliberate, not-yet-made decision rather than a default.

## "How do you know it actually works?"

243 automated tests (unit, API, mocked-provider, RAG, agent, evaluation-metrics — no test
makes a real network call or spends real money), plus a hand-curated, hand-verified 51-item
evaluation dataset that *does* run against the real database and real configured LLM provider:
retrieval Recall@K/MRR, end-to-end RAG-answer fact coverage and citation precision/completeness,
agent intent/tool-selection accuracy, and structured-extraction accuracy. Every dataset item was
built by directly reading real SEC filing text and writing the expected answer down *before*
running the system being evaluated — not generated by an LLM grading itself. Full methodology
and results in `docs/evaluation.md`.

## Anticipated deep-dive questions

**"Why RRF over a reranker for retrieval?"** Reciprocal Rank Fusion over independent vector and
keyword rankings is a well-established, dependency-free technique that's appropriate at this
project's scale (four tickers, ~2,200 chunks). A cross-encoder reranker is a reasonable next
step if retrieval quality demonstrably needs it beyond what a relevance floor/filtering would
buy — and the measured 35% Recall@5 result is exactly the kind of evidence that would justify
it, not a hypothetical.

**"Why not LangGraph / a framework for the agent?"** The orchestration is five clearly-scoped
stages with real branching logic (native tool-calling vs. a structured-planner fallback,
depending on provider capability) — implementing it directly kept every stage independently
testable and the control flow legible, without a framework's abstractions standing between the
code and what it's actually doing. Documented as a real tradeoff, revisited if the pipeline
ever needs genuine multi-turn tool loops.

**"Why four providers for one LLM integration?"** The project needed a real local dev provider
(DeepSeek, cost-effective) while staying provider-agnostic by design — a clean `LLMProvider`
ABC with capability metadata (`supports_structured_output`, `supports_tool_calling`,
`supports_streaming`), so the agent's planning stage branches on real capability rather than
assuming every provider behaves identically. This is what makes the structured-planner fallback
path meaningful, not dead code.

**"What's the one thing you'd point to as evidence of engineering judgment, not just AI
output?"** The evaluation framework catching its own construction mistakes, and the decision to
document those mistakes in the dataset notes rather than quietly fix and ship them. A portfolio
project's evaluation numbers are only worth something if the process that produced them is
itself trustworthy.
