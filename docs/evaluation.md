# Evaluation

This project's AI components (hybrid retrieval, RAG answering, agent orchestration,
structured extraction) are evaluated against a hand-curated, hand-verified dataset, not
graded impressionistically. Every number on this page is from a real run against the real
database, the real hybrid-search pipeline, and the real configured LLM provider — nothing
here is estimated or invented. See [engineering_decisions.md](engineering_decisions.md) for
why this framework looks the way it does, and [limitations.md](limitations.md) for what it
doesn't cover.

## Why a hand-curated dataset instead of a generated one

An LLM could have generated hundreds of Q&A pairs from the filing corpus in minutes. That
was deliberately not done here: a generated question's "ground truth" is only as trustworthy
as the model that generated it, which defeats the purpose of an evaluation meant to catch
that same class of error. Every item in this dataset was built by directly reading the real
SEC filing text for NVDA, AMD, MU, and SNDK (see `evaluation/datasets/*.jsonl` — each item
carries a `note` field citing the exact chunk(s) and quoting or paraphrasing the sentence the
label came from) and writing the expected answer down before ever running the system being
evaluated. 51 items across four categories, within the 40–100 range this project targeted for
V1 — enough to be a real signal, not so many that hand-verification became unreliable.

**A labeling mistake was caught and fixed during construction, not hidden.** The first pass
at `extraction_ground_truth.jsonl` asserted that none of the ingested filings contain
explicit forward-looking capex guidance, based on a search for phrases like "capex of
approximately". The real extraction run disagreed — it returned non-null capex figures for
three Micron filings. Investigating instead of dismissing the mismatch as a system error
found the actual sentences (e.g. "We estimate capital expenditures ... to be approximately
$27 billion in 2026" — a phrasing the initial search missed). The dataset was corrected to
assert the real figures instead of null, and the extraction system's numbers matched them
exactly. The same thing happened with four retrieval items, where the labeled "relevant
chunk" turned out to be one of two-or-three chunks that legitimately restate the same fact
(MD&A prose vs. a financial-statement note) — the dataset now lists all of them. Both
corrections are preserved in the `note` fields of the affected dataset items rather than
edited away, since a fabricated evaluation is worse than none.

## Methodology by category

### Retrieval (18 items, `evaluation/scripts/run_retrieval_eval.py`)

For each labeled `(query, ticker, relevant_chunk_ids)` item, runs the same `hybrid_search`
used by the `search_filings` agent tool and the `/research/documents` endpoint, scoped to the
item's ticker (mirroring how a well-formed tool call filters). No LLM calls — pure retrieval
quality, free to re-run. Scored with Recall@3/5/10 and Mean Reciprocal Rank against the
hand-verified relevant set (see `evaluation/metrics.py`).

### RAG answer (15 items, `run_answer_eval.py`)

Runs the real `rag.answer.answer_question` pipeline end-to-end (hybrid retrieval → context
assembly → one live LLM call). Two metrics:

- **fact_coverage**: deterministic, case-insensitive substring check of hand-picked
  `required_facts` (verbatim numbers/phrases from the source text) against the generated
  answer. This is intentionally blunt, not semantic — see "Known limitations" below.
- **citation_precision / citation_completeness**: computed by separately calling the same
  `hybrid_search(query, filters, k=6)` the pipeline uses internally, to recover chunk IDs
  (the `Citation` objects the pipeline returns are UI-facing and keyed by
  `(ticker, filing_date, section)`, not a raw chunk ID, since a section commonly spans many
  chunks — reversing one back to a single ID would be ambiguous).

One item (`ans-none-01`) is a deliberate negative control: a real question with no answer in
the ingested corpus (confirmed by construction — no share-repurchase-authorization figure was
found anywhere in Sandisk's ingested MD&A/liquidity text). It is scored for honest abstention
(does the answer say so, rather than inventing a number), not folded into fact_coverage.

### Agent orchestration (10 items, `run_agent_eval.py`)

Runs the real `AgentOrchestrator` — the same code path as `POST /research/query` — end to
end: intent classification, planning (native tool calling or the structured-planner
fallback), tool execution, evidence collection, synthesis, and verification. Checks:

- **intent_correct**: does the classified `IntentCategory` match the expected one.
- **tools_correct**: were the expected tool(s) actually called (a plan that also calls one
  extra reasonable tool still passes — this checks necessary tool usage, not exact-set
  equality). One item (`agt-10`, an out-of-scope question) expects *no* tool call at all, to
  confirm the agent doesn't force tool use onto unrelated questions.
- **latency / tokens / cost**: read directly from each run's real `ExecutionTrace` — nothing
  here is estimated separately from what the API would report to a real user.

This category also exercises the `compare_guidance` and `get_options_snapshot` tools'
honest-empty paths on purpose (`agt-05`, `agt-09`) — both tools are expected to report real
data limitations (only 2 guidance extractions exist for MU; no options-chain provider is
wired up) rather than fabricate a number, so "success" for those items means selecting the
right tool and getting an honest non-answer back, not getting a numeric result.

### Structured extraction (8 items, `run_extraction_eval.py`)

Runs the real `services.extraction.extract_guidance` (persists an `AIExtraction` row, same
as production — not a dry run) against one MD&A section per item, spanning both 10-K and
10-Q filings for all four tickers. Checks:

- **non_capex_fields_correctly_null**: revenue/EPS/gross-margin all extract as `None`,
  matching the confirmed absence of that guidance anywhere in the ingested corpus.
- **capex_correct**: capex matches the hand-verified expected value (or null, for the five
  filings that don't state one) within a small numeric tolerance. See the labeling-mistake
  note above for why this is a real, not rubber-stamped, check.
- **tone_plausible**: `management_tone.overall` is one of the hand-labeled acceptable tones
  for that filing (a semi-subjective judgment made while reading the real text — some filings
  mix clearly good news with a real risk disclosure, e.g. NVIDIA's FY2026 10-K discusses both
  65% revenue growth *and* a 3.9-point gross-margin decline, so both "positive" and "mixed"
  are accepted there).
- **key_driver_keyword_hit**: at least one extracted key driver / important topic contains an
  expected keyword.

## Results (most recent run)

Run at `2026-08-17T11:14:42Z` against `deepseek` / `deepseek-v4-flash`, embeddings via
local `BAAI/bge-small-en-v1.5`. Raw per-item output is written to
`evaluation/results/latest.json` (gitignored — regenerate with
`cd backend && uv run python ../evaluation/scripts/run_all.py`); the numbers below are
copied from that real run, not retyped from memory.

| Category | Metric | Value |
|---|---|---|
| Retrieval (18 items) | Mean Recall@3 | 0.306 |
| | Mean Recall@5 | 0.352 |
| | Mean Recall@10 | 0.380 |
| | Mean Reciprocal Rank | 0.269 |
| RAG answer (15 items) | Mean fact coverage | 0.700 |
| | Fully correct (all required facts stated) | 9 / 15 |
| | Mean citation precision | 0.067 |
| | Mean citation completeness | 0.367 |
| | Mean latency | 1.4s |
| Agent orchestration (10 items) | Intent classification accuracy | 100% |
| | Tool selection accuracy | 100% |
| | Verification run rate | 90% |
| | Mean latency | 5.0s |
| | Total cost (10 full agent runs) | $0.0175 |
| Structured extraction (8 items) | Non-capex fields correctly null | 100% |
| | Capex accuracy | 100% |
| | Tone plausibility | 100% |
| | Key-driver keyword hit rate | 100% |

## Honest analysis, not just numbers

**Retrieval is the real bottleneck, and it's a known, explainable one.** Recall@5 of 0.35 is
mediocre in isolation. Inspecting the actual `hybrid_search` output for the failing items
(not just the score) shows a consistent, specific cause: boilerplate section-opener text
("ITEM 1. FINANCIAL STATEMENTS Micron Technology, Inc. Consolidated Statements of
Operations...") and repeated numeric tables score competitively on both the vector and
keyword side of RRF for generic financial questions, crowding out the actual narrative MD&A
sentence that states the answer in prose. Micron is hit hardest — 0 of 3 MU retrieval items
land a top-5 hit — because its filings repeat that exact statement-of-operations header
across many chunks. This is the same root cause already documented in
[limitations.md](limitations.md) (no relevance floor, no reranker — RRF over two independent
rankings was chosen as scale-appropriate for ~2,200 chunks, not as a ceiling). This
evaluation is what makes that limitation a measured fact instead of a guess.

**When retrieval fails, the LLM abstains instead of fabricating — verified directly, not
assumed.** Two RAG-answer items (`ans-nvda-01`, `ans-nvda-02`) scored 0.0 fact coverage.
Reading the actual generated answers (not just the score) shows why: the needed chunk wasn't
in the top-6 retrieved, and the model said so explicitly — *"I cannot provide full fiscal
year 2026 data or a comparison to the full fiscal year 2025 gross margin"* and *"I cannot
determine how much NVIDIA spent on private company and infrastructure fund investments...
[the context] does not specify the amount spent."* Both are retrieval misses, not synthesis
hallucinations. The negative-control item (`ans-none-01`) confirms the same behavior on a
question with no answer anywhere in the corpus: the model states plainly that no buyback
authorization figure is mentioned rather than inventing one.

**Citation precision (0.067) looks alarming out of context — it's a metric-definition
artifact, not a false-citation rate.** `answer_question` always surfaces up to `k=6` chunks as
citations (so a user can see the full evidence set), while the hand-labeled relevant sets are
narrow (1–3 chunks per question, since most questions have one clearly correct source
passage). Precision is computed as `relevant ∩ cited / |cited|`, so even a "perfect" answer
that cites the one right chunk among 6 retrieved scores at most 1/6 ≈ 0.167 on this metric.
Citation completeness (0.367) — `relevant ∩ cited / |relevant|` — is the more informative
number for "did the system surface the right evidence," and even that is capped below 1.0 by
the same retrieval weakness described above, not by the citation-assembly step doing
anything wrong.

**Structured extraction (100% across the board) is the strongest result, and the labeling
mistake above is why it should be trusted.** Getting a null-guidance check right by predicting
"null" for everything would be trivial and uninformative. This dataset originally did exactly
that by mistake, was caught, and was corrected to require the extraction to distinguish
*absent* guidance from *present* guidance at the specific-number level (including the
difference between an "approximately $X" point estimate and an "above $X" open-ended bound,
which the model encoded correctly as `high=null`). That the corrected, harder version of the
check still passes 8/8 is a meaningfully stronger claim than the original all-null version
would have supported.

## Known limitations of this evaluation itself

- **fact_coverage is a substring match, not a semantic judge.** A factually correct answer
  that reformats a number (e.g. "$3.2B" instead of "$3.2 billion") registers as missing that
  fact. `required_facts` were chosen as short, distinctive tokens likely to survive a
  temperature-0.0 answer grounded in the provided context, but this is a real, structural
  false-negative risk, not a solved problem.
- **No LLM-as-judge layer.** The original project scope allowed one as a secondary signal.
  It wasn't added: a second model grading the first would add real cost and a second source of
  unverified claims, for a benefit (catching semantic-but-not-substring correctness) that the
  fact_coverage caveat above already documents honestly. A reasonable follow-up, not a gap
  hidden by omission.
- **Per-item cost/token accounting only exists for the agent category.** `AgentOrchestrator`
  returns a full `ExecutionTrace` with real usage and cost; `answer_question` and
  `extract_guidance` don't currently plumb `TokenUsage` back to their callers (the same
  documented gap as `generate_structured()` calls inside the agent pipeline itself — see
  [ai_architecture.md](ai_architecture.md)). The RAG-answer and extraction categories'
  real dollar cost was not zero, but this evaluation can't report an exact figure for them.
- **Retrieval and RAG-answer datasets share a lot of ticker/filing overlap** (both draw
  heavily on the same MD&A sections read during construction) rather than sampling
  independently across the full 2,231-chunk corpus. A larger, more broadly-sampled dataset
  is a reasonable Phase 9 follow-up, not something V1 needed to block on.
- **The negative-control and honest-empty checks are necessarily small** (one RAG-answer
  item, two agent items). They demonstrate the behavior exists and is correct where tested,
  not that it holds universally.

## Running the evaluation

```bash
cd backend
uv run python ../evaluation/scripts/run_retrieval_eval.py    # no LLM calls, free
uv run python ../evaluation/scripts/run_answer_eval.py       # ~15 live LLM calls
uv run python ../evaluation/scripts/run_agent_eval.py        # ~10 full agent runs, several LLM calls each
uv run python ../evaluation/scripts/run_extraction_eval.py   # ~8 live LLM calls
uv run python ../evaluation/scripts/run_all.py                # all four, writes evaluation/results/
```

Metric functions (`recall_at_k`, `mean_reciprocal_rank`, `citation_precision`,
`citation_completeness`, `fact_coverage`) are pure and unit-tested independently of any live
system in `backend/tests/test_evaluation_metrics.py` — `cd backend && uv run pytest
tests/test_evaluation_metrics.py`.
