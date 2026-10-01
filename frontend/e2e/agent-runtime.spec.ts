import { expect, test, type Page, type Route } from "@playwright/test";

/**
 * The agentic research workflow surface (Phase LG-1).
 *
 * The properties pinned here are the ones a reader would be misled by if
 * they broke: a legacy answer shows NO workflow at all rather than an
 * invented verdict; a reader never meets framework jargon outside
 * Advanced; a retry round is explained in terms of what was searched, not
 * which node ran; and the Operations card states that neither runtime can
 * reach the V4 forward path.
 *
 * Fully mocked; never reaches a real LLM, TWS or production database.
 */
const json = (body: unknown) => (route: Route) => route.fulfill({ json: body });
const NOW = "2026-10-01T12:00:00+00:00";

const item = (over: Record<string, unknown> = {}) => ({
  id: 1,
  ticker: "MU",
  question: "What did the latest filing say about margins?",
  answer_markdown: "Gross margin expanded on favourable mix. [1]",
  citations: [
    {
      marker: "[1]",
      ticker: "MU",
      filing_type: "10-Q",
      filing_date: "2026-02-18",
      section: "Item 7",
      source_url: "https://www.sec.gov/example",
    },
  ],
  intent_category: "filing_research",
  planning_method: "structured_planner",
  tool_calls: [
    {
      tool_name: "filings_search",
      arguments: {},
      success: true,
      duration_ms: 120,
      summary: "4 chunks",
      error: null,
      query_description: "margin",
    },
  ],
  verification_ran: true,
  verification_supported: true,
  revised: false,
  provider: "deepseek",
  model: "deepseek-v4-flash",
  total_input_tokens: 1200,
  total_output_tokens: 300,
  estimated_cost_usd: "0.0010",
  total_duration_ms: 4200,
  created_at: NOW,
  ...over,
});

const node = (name: string, over: Record<string, unknown> = {}) => ({
  node: name,
  started_at: NOW,
  finished_at: NOW,
  duration_ms: 42,
  status: "ok",
  llm_calls: 0,
  tool_calls: 0,
  attempt: 1,
  ...over,
});

const workflow = (over: Record<string, unknown> = {}) => ({
  runtime_version: "langgraph-agent-v1",
  graph_version: "edl-research-graph-v1",
  run_id: "11111111-2222-3333-4444-555555555555",
  retrieval_rounds: 0,
  revision_count: 0,
  llm_calls: 4,
  evidence_quality: {
    status: "sufficient",
    missing_categories: [],
    weak_categories: [],
    conflicts: [],
    recommended_retrieval: [],
    explanation: "The evidence collected covers what this question needs.",
  },
  node_runs: [
    node("classify_intent", { llm_calls: 1 }),
    node("window_context", { status: "skipped" }),
    node("plan_research", { llm_calls: 1 }),
    node("execute_tools", { tool_calls: 1 }),
    node("merge_evidence"),
    node("evidence_quality_gate"),
    node("synthesize", { llm_calls: 1 }),
    node("verify", { llm_calls: 1 }),
  ],
  errors: [],
  warnings: [],
  checkpoint_thread_id: null,
  needs_refresh_for_window: null,
  legal_decision_at: null,
  ...over,
});

const runtimeStatus = (over: Record<string, unknown> = {}) => ({
  configured_runtime: "legacy",
  runtime_version: "legacy-agent-v1",
  graph_version: "edl-research-graph-v1",
  nodes: [
    "classify_intent",
    "window_context",
    "plan_research",
    "execute_tools",
    "merge_evidence",
    "evidence_quality_gate",
    "targeted_retrieve",
    "synthesize",
    "verify",
    "revise",
  ],
  conditional_routes: {
    evidence_quality_gate: ["targeted_retrieve", "synthesize"],
    verify: ["revise", "end"],
  },
  max_retrieval_rounds: 1,
  max_revisions: 1,
  max_tool_calls: 6,
  tools: [
    { name: "search_filings", access: "read_only", evidence_category: "filing" },
    { name: "calculate_implied_move", access: "derived_calculation", evidence_category: "derived" },
  ],
  checkpointing_enabled: false,
  checkpoint_available: false,
  checkpoint_schema: "langgraph",
  checkpoint_detail: "checkpointer not initialised yet (missing checkpoints)",
  warning: null,
  ...over,
});

/** Waits for the Operations shell before asserting on any card.
 *
 * Matches e2e/operations.spec.ts's own first assertion: that page fires a
 * dozen requests and, against the dev server, compiles on first visit, so
 * going straight to a card's test id races the page rather than testing
 * it. */
async function openOperations(page: Page) {
  await page.goto("/operations");
  // 30s, matching playwright.config.ts's own server timeouts. This page
  // fires a dozen requests and, on its first visit in a worker, the dev
  // server compiles it from source -- a cost measured at over the default
  // 5s. The property under test is what the card SAYS, so absorbing page
  // load here is correct; shortening it would only make the suite flaky
  // about something it is not testing.
  await expect(page.getByRole("heading", { name: "Live Operations" })).toBeVisible({
    timeout: 30_000,
  });
  await expect(page.getByTestId("operations-agent-runtime")).toBeVisible({ timeout: 30_000 });
}

async function ask(page: Page, response: Record<string, unknown>) {
  await page.route("**/research/query", json(response));
  await page.route("**/research/history?*", json([item()]));
  await page.goto("/research");
  await page.getByPlaceholder(/Ask about a covered company/).fill("margins?");
  await page.locator("button.btn").first().click();
}

const completed = (over: Record<string, unknown> = {}) => ({
  question: "margins?",
  status: "completed",
  answer: "x",
  citations: [],
  trace: null,
  preparing: [],
  unresolved_tickers: [],
  agent_runtime: "legacy-agent-v1",
  workflow: null,
  ...over,
});

// --- a legacy answer claims nothing it did not do ----------------------

test("a legacy answer shows no workflow panel at all", async ({ page }) => {
  await ask(page, completed());
  await expect(page.getByTestId("answer-header")).toContainText("MU");
  // Legacy has no evidence-quality gate. The panel is absent rather than
  // showing a verdict nothing reached.
  await expect(page.getByTestId("research-workflow")).toBeHidden();
  await page.getByText("Advanced details").click();
  await expect(page.getByTestId("workflow-diagnostics")).toBeHidden();
});

// --- a graph answer explains itself in plain language ------------------

test("a graph answer names its steps without framework jargon", async ({ page }) => {
  await ask(page, completed({ agent_runtime: "langgraph-agent-v1", workflow: workflow() }));
  const panel = page.getByTestId("research-workflow");
  await expect(panel).toContainText("Understanding the question");
  await expect(panel).toContainText("Collecting evidence");
  await expect(panel).toContainText("Checking evidence coverage");
  await expect(panel).toContainText("Verification");
  await expect(page.getByTestId("evidence-quality-status")).toContainText("Evidence covered");
  // Requirement 64: no node identifiers, no framework names, outside
  // Advanced.
  await expect(panel).not.toContainText("evidence_quality_gate");
  await expect(panel).not.toContainText("LangGraph");
  await expect(panel).not.toContainText("langgraph-agent-v1");
});

test("a step that was not needed says so instead of looking broken", async ({ page }) => {
  await ask(page, completed({ agent_runtime: "langgraph-agent-v1", workflow: workflow() }));
  const panel = page.getByTestId("research-workflow");
  await expect(panel).toContainText("Checking research freshness");
  await expect(panel).toContainText("not needed");
  await expect(panel).not.toContainText("failed");
});

test("a retry round is explained by what was searched, not by which node ran", async ({ page }) => {
  await ask(
    page,
    completed({
      agent_runtime: "langgraph-agent-v1",
      workflow: workflow({
        retrieval_rounds: 1,
        evidence_quality: {
          status: "partial",
          missing_categories: [],
          weak_categories: ["filing"],
          conflicts: [],
          recommended_retrieval: ["filing"],
          explanation: "Only thin evidence for filing.",
        },
        node_runs: [
          node("evidence_quality_gate"),
          node("targeted_retrieve", { tool_calls: 1, attempt: 2 }),
          node("evidence_quality_gate", { attempt: 2 }),
          node("synthesize", { llm_calls: 1 }),
        ],
      }),
    }),
  );
  const panel = page.getByTestId("research-workflow");
  await expect(panel).toContainText("Partial evidence");
  await expect(panel).toContainText("SEC filings was searched again");
  await expect(panel).toContainText("Retrying missing evidence");
  // The gate ran twice; that is one step that happened twice.
  await expect(panel).toContainText("Checking evidence coverage · ran 2×");
});

test("contradictory evidence is surfaced rather than resolved by guessing", async ({ page }) => {
  await ask(
    page,
    completed({
      agent_runtime: "langgraph-agent-v1",
      workflow: workflow({
        evidence_quality: {
          status: "partial",
          missing_categories: [],
          weak_categories: [],
          conflicts: [
            {
              category: "estimates",
              description:
                "consensus provider expects the report on 2026-10-20, but the earnings calendar has 2026-10-22",
            },
          ],
          recommended_retrieval: [],
          explanation: "Two pieces of evidence disagree.",
        },
      }),
    }),
  );
  await expect(page.getByTestId("evidence-conflict")).toContainText("2026-10-22");
});

test("framework detail is available under Advanced and only there", async ({ page }) => {
  await ask(page, completed({ agent_runtime: "langgraph-agent-v1", workflow: workflow() }));
  await expect(page.getByTestId("workflow-diagnostics")).toBeHidden();
  await page.getByText("Advanced details").click();
  const diagnostics = page.getByTestId("workflow-diagnostics");
  await expect(diagnostics).toContainText("langgraph-agent-v1");
  await expect(diagnostics).toContainText("edl-research-graph-v1");
  await page.getByText("Workflow node timings").click();
  await expect(page.getByText("evidence_quality_gate")).toBeVisible();
  await expect(page.getByText("not persisted for this run")).toBeVisible();
});

// --- Operations -------------------------------------------------------

test("the Operations card states the forward path is unaffected", async ({ page }) => {
  await page.route("**/research/agent-runtime", json(runtimeStatus()));
  await openOperations(page);
  const card = page.getByTestId("operations-agent-runtime");
  await expect(card).toContainText("LEGACY");
  await expect(card).toContainText("Neither reaches the V4 forward decision path");
  await expect(card).toContainText("read-only or pure calculation");
});

test("Operations reports bounded loops and resume state honestly", async ({ page }) => {
  await page.route("**/research/agent-runtime", json(runtimeStatus()));
  await openOperations(page);
  const card = page.getByTestId("operations-agent-runtime");
  await expect(card).toContainText("Retry rounds");
  await expect(card).toContainText("Revisions");
  // Checkpointing off reads as "Off", never as a failure.
  await expect(card).toContainText("Off");
  await expect(card).not.toContainText("Not initialised");
});

test("a misconfigured runtime flag is reported, not hidden", async ({ page }) => {
  await page.route(
    "**/research/agent-runtime",
    json(
      runtimeStatus({
        warning: "AGENT_RUNTIME is set to 'langraph', which is not a known runtime; using 'legacy'.",
      }),
    ),
  );
  await openOperations(page);
  await expect(page.getByTestId("agent-runtime-warning")).toContainText("not a known runtime");
  await expect(page.getByTestId("operations-agent-runtime")).toContainText("LEGACY");
});

test("the workflow shape and tool classification are inspectable", async ({ page }) => {
  await page.route("**/research/agent-runtime", json(runtimeStatus()));
  await openOperations(page);
  await page.getByText("Workflow shape and tool classification").click();
  const card = page.getByTestId("operations-agent-runtime");
  await expect(card).toContainText("classify_intent → window_context");
  await expect(card).toContainText("evidence_quality_gate → targeted_retrieve | synthesize");
  await expect(card).toContainText("search_filings · read only");
});
