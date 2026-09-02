import { test, expect, type Route } from "@playwright/test";

/**
 * AI Research states (Section 60): ready answer with provenance, preparing,
 * company not found, insufficient evidence. Fully mocked; never reaches a
 * real LLM, TWS or production database.
 */
const json = (body: unknown) => (route: Route) => route.fulfill({ json: body });
const NOW = "2026-09-02T12:00:00+00:00";

const item = (over: Record<string, unknown> = {}) => ({
  id: 1, ticker: "PANW", question: "What did PANW guide?", answer_markdown: "**Guidance** was raised. [1]",
  citations: [{ marker: "[1]", ticker: "PANW", filing_type: "10-Q", filing_date: "2026-05-20", section: "MD&A", source_url: "https://www.sec.gov/example" }],
  intent_category: "guidance", planning_method: "structured",
  tool_calls: [
    { tool_name: "filings_search", arguments: {}, success: true, duration_ms: 120, summary: "3 chunks", error: null, query_description: "guidance" },
    { tool_name: "earnings_history", arguments: {}, success: true, duration_ms: 30, summary: "8 events", error: null, query_description: null },
  ],
  verification_ran: true, verification_supported: true, revised: false, provider: "deepseek", model: "deepseek-v4-flash",
  total_input_tokens: 1200, total_output_tokens: 300, estimated_cost_usd: "0.0010", total_duration_ms: 4200, created_at: NOW,
  ...over,
});

async function ask(page: import("@playwright/test").Page, q: string) {
  await page.goto("/research");
  await page.getByPlaceholder(/Ask about a covered company/).fill(q);
  await page.locator("button.btn").first().click();
}

test("ready answer shows company, as-of, grounding and human-readable evidence sources", async ({ page }) => {
  await page.route("**/research/query", json({ question: "What did PANW guide?", status: "completed", answer: "x", citations: [], trace: null, preparing: [], unresolved_tickers: [] }));
  await page.route("**/research/history?*", json([item()]));
  await ask(page, "What did PANW guide?");
  await expect(page.getByTestId("answer-header")).toContainText("PANW");
  await expect(page.getByTestId("grounding-status")).toContainText("Grounded");
  await expect(page.getByTestId("evidence-sources")).toContainText("10-Q");
  await expect(page.getByTestId("evidence-sources")).toContainText("Earnings history");
  await expect(page.getByRole("link", { name: "Inspect source" })).toHaveAttribute("href", "https://www.sec.gov/example");
  // Vector-DB internals only under Advanced.
  await expect(page.getByText("Retrieval query")).toBeHidden();
  await page.getByText("Advanced details").click();
  await expect(page.getByText("Retrieval query")).toBeVisible();
});

test("insufficient evidence still renders the persisted answer with an honest grounding label", async ({ page }) => {
  await page.route("**/research/query", json({ question: "q", status: "insufficient_evidence", answer: null, citations: [], trace: null, preparing: [], unresolved_tickers: [] }));
  await page.route("**/research/history?*", json([item({ citations: [], verification_ran: false, answer_markdown: "Not enough evidence in the prepared filings to answer this." })]));
  await ask(page, "q");
  await expect(page.getByTestId("grounding-status")).toContainText("No filing citations");
  await expect(page.getByTestId("evidence-sources")).toContainText("Earnings history");
});

test("company not found is a plain honest notice", async ({ page }) => {
  await page.route("**/research/query", json({ question: "q", status: "company_not_found", answer: null, citations: [], trace: null, preparing: [], unresolved_tickers: ["ZZZZ"] }));
  await ask(page, "What about ZZZZ?");
  await expect(page.getByText(/ZZZZ doesn't look like a real, SEC-listed company/)).toBeVisible();
});

test("preparing shows queue position and current step from real progress", async ({ page }) => {
  await page.route("**/research/query", json({ question: "q", status: "preparing", answer: null, citations: [], trace: null, preparing: [{ ticker: "PANW", job_id: 9, job_status: "running" }], unresolved_tickers: [] }));
  await page.route("**/operations/preparation-progress", json({ queue_depth: 1, completed: 2, failed: 0, worker_active: true, current_symbol: "PANW", current_stage: "filings", step_index: 3, step_total: 6, attempt: 1, heartbeat_seconds_ago: 4, elapsed_seconds: 40 }));
  await ask(page, "What did PANW guide?");
  await expect(page.getByTestId("research-preparing")).toContainText("Preparing research for PANW");
  await expect(page.getByTestId("research-preparing")).toContainText("Running");
  await expect(page.getByTestId("research-preparing")).toContainText("filings (3/6)");
  await expect(page.getByTestId("research-preparing")).toContainText("Last progress 4s ago");
});
