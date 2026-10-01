# Agentic research architecture

LangChain and LangGraph in Earnings Decision Lab — what they are for, what they are
deliberately not for, and where the boundary sits.

**Status: Phase LG-1, interactive research only.** `AGENT_RUNTIME` defaults to `legacy`.
The official V4 forward decision path does not pass through either runtime in either
position.

---

## The one-sentence version

LangChain standardizes how this project talks to models, tools and schemas. LangGraph
makes the research workflow an explicit, recoverable state machine. Neither touches the
deterministic financial engine, and neither decides a strike, a size or a price.

```
                        USER / SCHEDULER
                               │
                    ┌──────────▼──────────┐
                    │   LANGGRAPH         │  state, routing, bounds, checkpoints
                    │   research graph    │
                    └──────────┬──────────┘
                               │
                    ┌──────────▼──────────┐
                    │   LANGCHAIN         │  model / tool / schema protocols
                    │   adapters          │
                    └──────────┬──────────┘
                               │
            ┌──────────────────┼──────────────────┐
            │                  │                  │
      services/llm/       agents/tools/        rag/
      (DeepSeek,          (7 read-only         (SEC-aware chunks,
       OpenAI,             services)            pgvector + FTS, RRF)
       Anthropic)
                               │
                   ═══════ DecisionView boundary ═══════
                               │
                    DETERMINISTIC FINANCIAL ENGINE
            expected move · strike geometry · candidates
            T+1 valuation · stress · ranking · capital · sizing
                               │
                      Immutable evidence
                               │
                       Forward testing
```

---

## Why LangChain exists here

Not for the loaders, splitters or vector-store adapters — this project has better,
SEC-specific versions of all three. For three protocols:

| Protocol | What it buys |
|---|---|
| `BaseChatModel` | The graph composes a real Runnable instead of a bespoke callable |
| `StructuredTool` | Tools carry their own schema and a typed artifact alongside their text |
| `BaseRetriever` | Retrieval is an interface the graph (or anything later) can accept |

Three packages, not the `langchain` meta-package: `langchain-core`, `langgraph`,
`langgraph-checkpoint-postgres`.

### The adapter points the other way on purpose

`agents/adapters/model.py` wraps `services.llm.base.LLMProvider`. It does **not** replace
it with `langchain-deepseek`. That direction is a decision, not inertia — a native
integration would have silently dropped:

- `thinking` / `reasoning_effort` as a **fail-closed** request: `LLMConfigurationError`
  before any HTTP call, never a quiet downgrade to the API's default reasoning mode
  (`services/v4_decision_view_config.py` depends on this);
- `reasoning_tokens`, `prompt_cache_hit_tokens`, `prompt_cache_miss_tokens` — which
  DeepSeek reports and which this project bills and reports on;
- `reasoning_present` / `reasoning_chars`: the **presence and size** of hidden reasoning,
  recorded without ever storing the reasoning text;
- the real per-provider structured-output normalization (JSON mode plus prompt for one
  provider, a forced tool call for another);
- the error taxonomy in `services/llm/errors.py`, which the API layer maps to HTTP states.

So `with_structured_output` **delegates** to `generate_structured` rather than deriving its
own strategy. There is one answer per provider to "how is structured output requested
here", and it is not in this layer.

The adapter also refuses rather than pretending, in three places: a `stop` sequence
raises (the provider abstraction has no such field, and a model that appears to respect a
constraint it never received is worse than one that says it cannot); `bind_tools` raises on
a provider without tool calling; `with_structured_output` raises on a non-pydantic schema.

---

## Why LangGraph exists here

The legacy orchestrator is a correct, readable, linear pipeline. What it cannot do:

1. **Resume.** A run that fails at synthesis after four successful tool calls starts over.
2. **Route on evidence quality.** There was no point at which the pipeline asked "is this
   enough to answer honestly?", so it never asked for the one missing piece.
3. **Make its own bounds inspectable.** The revision limit was a structural property of a
   method body rather than a value the UI could state.

LangGraph provides state, conditional routing and checkpointing for exactly those three.

### The graph

```
START
  → classify_intent          1 LLM call, best-effort → GENERAL on failure
  → window_context           0 LLM calls, no-op without a calendar event
  → plan_research            1 LLM call, branches on supports_tool_calling
  → execute_tools            ≤ 6 tool calls, each isolated
  → merge_evidence           0 LLM calls, idempotent
  → evidence_quality_gate    0 LLM calls, fully deterministic
       ├ SUFFICIENT ·······························→ synthesize
       ├ PARTIAL/INSUFFICIENT + a round left ·······→ targeted_retrieve
       │                                               → merge_evidence
       │                                               → evidence_quality_gate
       └ PARTIAL/INSUFFICIENT + none left ··········→ synthesize
  → synthesize               1 LLM call, skipped with no evidence
  → verify                   1 LLM call, best-effort
       ├ supported, or unavailable ·················→ END
       ├ unsupported + a revision left ·············→ revise → verify
       └ unsupported + none left ···················→ END
END
```

### Bounds

| Bound | Value | Enforced by |
|---|---|---|
| Tool calls per run | 6 | `MAX_TOOL_CALLS`, unchanged from legacy |
| Targeted retrieval rounds | 1 | `MAX_RETRIEVAL_ROUNDS`, checked in `route_after_quality_gate` |
| Revisions | 1 | `MAX_REVISIONS`, checked in `route_after_verify` |
| Structured-output attempts | 2 | `MAX_STRUCTURED_ATTEMPTS`, parse failures only |

Every bound is a counter in the state checked by a routing function — not a recursion
limit, and not a prompt asking the model to stop. `GET /research/agent-runtime` reports
them from the same constants the graph is compiled from, so a documented limit cannot drift
from the one in force.

### State

`agents/graph/state.py`. Every value is JSON-safe: a checkpoint is something an operator
may have to read in `psql` on a window day. The database session, HTTP client and loaded
embedder live in `GraphDeps`, which is never checkpointed.

Five lists are append-only (`operator.add`) so a retrieval round adds to the record rather
than overwriting it. `evidence_blocks` and `citations` are deliberately **not** additive:
`merge_evidence` recomputes them from the full tool record each time, which keeps that node
idempotent across rounds.

---

## The evidence-quality gate

**Zero LLM calls.** "Do we have SEC evidence?", "did that tool succeed with no rows?", "is
this filing dated after the run's own cutoff?" are facts about collected state. A model
asked to judge them would be a slower, costlier and less reliable way to read a list. A
test reads `agents/graph/quality.py` and fails if a model call appears in it.

Intent-specific requirements, because an earnings-history question is not short of evidence
because nobody fetched an options chain:

| Intent | Required category | Citations required |
|---|---|---|
| `earnings_history` | earnings history | no |
| `filing_research` | filings | yes |
| `guidance_comparison` | guidance | yes |
| `options_analytics` | options context | no |
| `general` | none — the bar is that *something* returned data | no |

Verdicts:

- **INSUFFICIENT** — a required category produced nothing, or nothing anywhere did, or
  every tool failed.
- **PARTIAL** — required categories present but something real is wrong: thin, uncited, or
  contradictory.
- **SUFFICIENT** — go ahead.

`success=True` with zero rows is this project's honest "no data available" answer, so it is
reported as **weak**, never as missing. An earnings sample below four quarters is weak too:
three events are real data that still cannot describe a pattern.

### Conflicts it detects, and the one it does not

Deterministically detected:

- **Point-in-time violation** — a citation dated after the run's own `as_of`. Retrieval
  filters on `filing_date_to` already, so reaching this means something bypassed the
  filter.
- **Event-timing disagreement** — the consensus provider's expected report date against the
  calendar event's, when the run names one.

**Not** detected: semantic contradiction between two filings' prose. That is where LLM
judgement would genuinely help, and the gate reports nothing rather than guessing. This is
a named, deferred extension — not a silent gap. Inventing a conflict would be worse than
reporting none.

### Targeted retrieval

Retries the **gap only** (requirement 29), never the whole plan, and spends zero LLM calls:
which tool serves which category is a fact about this codebase, written down in
`_RETRY_TOOL_BY_CATEGORY`. A conflict alone is never retried — re-fetching the same
contradictory rows buys the same contradiction and spends budget to do it.

Filing retries go through the retriever adapter with a wider `k` rather than repeating an
identical tool call, because an identical search returns the identical nothing.

---

## What remains custom

The RAG stack, unchanged and unwrapped except by an interface:

| Kept | Where |
|---|---|
| SEC-aware parsing | `rag/parsing.py` |
| Section-bounded, token-approximate chunking | `rag/chunking.py` |
| Local fastembed embeddings (no API key) | `rag/embeddings.py` |
| pgvector cosine + Postgres FTS, fused by RRF | `rag/retrieval.py` |
| Company, filing-type and as-of metadata filters | `rag/retrieval.py` |
| Structured citations with accession numbers | `rag/context.py` |

`agents/adapters/retriever.py` converts `RetrievedChunk` → `Document` and nothing else. Its
metadata carries exactly the fields `rag.context.Citation` is built from, so a citation
produced through the adapter is the same citation as before.

There is **no** generic `RecursiveCharacterTextSplitter` and no vector-only path. Replacing
hybrid retrieval would need a benchmark showing improvement, not a framework default.

---

## What remains deterministic

Everything below the DecisionView boundary. AI produces judgement; Python produces
arithmetic.

An agent never chooses: a strike · an expiration · a quantity · a ranking score · a T+1
payoff · a Black–Scholes value · a max loss · a capital allocation · an entry price · a
settlement price.

Enforced structurally by `tests/test_agent_runtime_isolation.py`, which walks the real
import graph with `ast` (deferred imports included — this codebase uses them heavily):

- no module in the research layer imports `analytics.decision.*`, `services.v4_*` or
  `analytics.options.strategy_candidates`;
- no module references `black_scholes`, `max_defined_risk`, `position_size`,
  `choose_v4_2_candidate`, `rank_candidates` or `compute_entry_exit_schedule`;
- no official forward module imports either runtime, and the positive half asserts
  `default_view_generator` still calls the provider directly — so a refactor that routes it
  through the graph fails there.

---

## The V4 boundary

`services/v4_shadow_orchestration.py::default_view_generator` generates the official
forward DecisionView by calling `LLMProvider.generate_structured_result` **directly**. It
has never used `AgentOrchestrator`, and it does not use the graph.

That is why `AGENT_RUNTIME` cannot reach it in either position, and why Phase LG-1 is safe
to run while V4.2 Phase 2 is awaiting its own activation: changing research orchestration
and candidate search in the same period would confound the two.

The graph **is** capable of producing the canonical `DecisionView` schema — asserted in
`tests/test_agent_structured_output.py` — and that capability is for parity work only.

### No critic chain

One controlled judgement plus one bounded verification is the methodology. There is no bull
agent, bear agent, risk agent, critic or judge, and a test fails if a node name contains
any of those words. Adding adversarial roles would be a methodology change, not an
implementation change.

---

## Checkpoint model

| Decision | Why |
|---|---|
| Postgres, not Redis | This stack already runs Postgres and already depends on `psycopg[binary]`. Redis would be a service to deploy, monitor and back up, bought for no capability the graph needs. |
| Dedicated `langgraph` schema | A checkpoint is operational state and must never be mistaken for financial evidence. The separation is physical: all four tables land there, none in `public`. |
| `PostgresSaver.setup()`, not Alembic | The checkpointer runs its own versioned migrations. Re-declaring its tables in Alembic would be two owners for one schema, and the first upstream change would desynchronize them. |
| `migrations/env.py` ignores the schema | Autogenerate can neither drop nor recreate them. |
| Off by default | `AGENT_GRAPH_CHECKPOINTING_ENABLED=false`. Separate from `AGENT_RUNTIME` because the questions are separate — a graph run is useful without resume, and checkpointing adds real writes. |

### Thread identity

Two namespaces that cannot collide (a UUID contains no colon):

- `research:{run_id}` — one interactive question. A resume means "finish answering *this*
  question", so a new question is a new thread.
- `prep:{company_id}:{event_id}:{as_of}` — deterministic, so retrying the *same*
  preparation resumes instead of restarting. `as_of` is in the key because the same event
  at a different cutoff is a different run, and sharing a thread would let one overwrite
  the other's state.

### Resume

`invoke(None, config)` continues from the pending node. Completed nodes are not
re-executed — asserted by counting real provider and tool calls across a failure and a
resume, not by trusting the framework.

A resumed process has no `ToolOutcome` objects (they are not checkpointed), so
`merge_evidence` carries the checkpointed block forward. It distinguishes that from a tool
that **raised**, which has no outcome either but must still produce a `### tool — FAILED`
block so synthesis is told about the gap. Conflating those two cases was a real bug caught
by a test on this branch.

---

## Failure model

Domain-specific, never collapsed into one `AgentError`. A `GraphError` carries the real
exception class name from `services/llm/errors.py` or the failing service, plus whether
retrying could plausibly help and whether a resume has somewhere to resume from.

| Stage | On failure |
|---|---|
| `classify_intent` | Degrades to GENERAL, records a warning the user sees |
| `plan_research` | Ends the run with an honest message, not an empty answer |
| any tool | Recorded as a failed call; synthesis is told about the gap |
| `synthesize` | Honest "could not complete this answer", error recorded |
| `verify` | Draft stands, warning says it was not checked |
| `revise` | Previous answer stands |

Quota exhaustion is **not** retried. Only `StructuredOutputError` is — a missing key or an
unreachable provider is not a parse problem, and retrying it spends quota to get the same
answer.

Error messages pass through `observability/redact.py` before being stored: a provider error
can echo a request URL, and this project's adapters authenticate via a query parameter.

---

## Observability

`node`, `started_at`, `finished_at`, `duration_ms`, `status`, `llm_calls`, `tool_calls`,
`attempt`. That is the whole shape, and a test asserts the exact key set, so a prompt or a
model output cannot be added to it by accident.

**No hidden chain-of-thought is persisted.** Only that reasoning was present and how large
it was. No module in the research layer references `reasoning_content`.

langsmith arrives transitively with `langchain-core`. Tracing would send prompts and tool
output to a third-party service, so it is off by default and nothing in this codebase can
turn it on — asserted by a test.

---

## Runtime versioning

| Identity | Value |
|---|---|
| Legacy runtime | `legacy-agent-v1` |
| Graph runtime | `langgraph-agent-v1` |
| Graph shape | `edl-research-graph-v1` |

Every answer reports `agent_runtime`, so provenance never has to be inferred from whether
the `workflow` block happens to be present. Historical rows are never rewritten.

`AGENT_RUNTIME` is a plain string, not a `Literal`. `get_settings()` is read by essentially
every request, so a `Literal` would turn a typo in an experimental flag into a 422 on every
endpoint in the API — health check and V4 operations pages included. `agents/runtime.py` is
the one place that interprets the value; an unrecognized one falls back to legacy and says
so in the status endpoint's `warning`.

---

## Tool classification

All seven research tools are read-only or pure calculation. Three guarantees are
structural, not documented:

1. An unclassified tool raises at registry-build time rather than defaulting to "probably
   fine".
2. Nothing classified `WRITE` is exposed; production evidence writes stay in application
   code the graph calls.
3. A tool whose name describes brokerage execution is refused outright, by substring — so
   the boundary survives someone adding `place_order` later in good faith, including the
   well-meaning `submit_bracket_order_dry_run`.

| Tool | Access | Evidence category |
|---|---|---|
| `search_filings` | READ_ONLY | filing |
| `get_historical_earnings` | READ_ONLY | earnings_history |
| `get_analyst_estimates` | READ_ONLY | estimates |
| `compare_guidance` | READ_ONLY | guidance |
| `get_options_snapshot` | READ_ONLY | options_context |
| `calculate_strategy_payoff` | DERIVED_CALCULATION | derived |
| `calculate_implied_move` | DERIVED_CALCULATION | derived |

**No brokerage-execution tool exists.** No place order, modify order, cancel order or
exercise, in any form, anywhere in `agents/`.

---

## Research freshness at the legal decision window

`window_context` preserves the 2026-09-24 fix by **calling** it, not reimplementing it:
`services/earnings_research_preparation.py::v4_research_ready(as_of=legal_decision_window(event))`.
Reimplementing that comparison is precisely how the two would drift apart again.

When research is fresh now but will be stale at the window, the run says so. It never acts
on it — refreshing research is the preparation pipeline's job.

---

## Parity

`src/evaluation/agent_parity.py` runs both runtimes on the same seeded rows and the same
script (two independently-constructed providers loaded identically — a shared instance
would let the first runtime drain the queue).

Prose is never compared: an LLM is not reproducible in general, and demanding identical
wording would turn a true pass into a flake. Structure and behaviour are.

The harness separates **declared** differences from **regressions**. A declared difference
is one the graph is designed to produce, with the precondition under which it is allowed:

| Field | Allowed when | Because |
|---|---|---|
| `tools_called`, `tools_succeeded`, `citation_count` | `retrieval_rounds > 0` | the gate asked a follow-up legacy has no mechanism to ask |
| `verification_supported` | `revised` | the graph re-verifies a revised answer and reports the verdict on what it returns; legacy reports the pre-revision verdict about an answer it already replaced |

The same difference on a run where the precondition did **not** hold is still a failure.
Silently excluding these fields would have hidden both the feature and any future
regression in them.

---

## Deployment phases

| Phase | Contents | State |
|---|---|---|
| LG-0 | Adapters, no behaviour change | done |
| LG-1 | Graph for interactive research, flag default legacy | done, not activated |
| LG-1 activation | `AGENT_RUNTIME=langgraph` for interactive AI Research only | requires a person to read the parity table |
| LG-2 | Automatic pre-earnings research preparation | **not started**, separate review required |

LG-2 is not a continuation of LG-1. It changes what the automated pipeline does and needs
its own review before production activation.

---

## Files

| File | Role |
|---|---|
| `agents/adapters/model.py` | `BaseChatModel` over `LLMProvider` |
| `agents/adapters/tools.py` | `StructuredTool` adapters, access classification, execution refusal |
| `agents/adapters/structured.py` | Bounded, observable structured output |
| `agents/adapters/retriever.py` | `BaseRetriever` over `hybrid_search` |
| `agents/graph/state.py` | `ResearchState`, runtime identities, bounds |
| `agents/graph/deps.py` | Non-checkpointed per-request dependencies |
| `agents/graph/quality.py` | The deterministic evidence-quality gate |
| `agents/graph/nodes.py` | The ten nodes |
| `agents/graph/workflow.py` | Graph construction and the two conditional routes |
| `agents/graph/runtime.py` | Running, resuming, and `AgentResponse` conversion |
| `agents/graph/checkpoint.py` | Postgres checkpointer in the `langgraph` schema |
| `agents/runtime.py` | Runtime selection and versioning |
| `agents/evidence.py` | Helpers shared by **both** runtimes |
| `evaluation/agent_parity.py` | The parity harness |
