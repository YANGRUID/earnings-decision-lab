import { test, expect, type Route } from "@playwright/test";

/**
 * Long-list controls (2026-09-02). Pages that render a growing dataset
 * (the Operations pipeline already shows 100+ rows) get the same four
 * controls -- search, one facet, sort, paging -- with state in the URL and
 * "All" always available. Every response here is a hand-labeled fixture
 * served by page.route(); nothing talks to a real backend.
 */

const LIFECYCLES = ["CALENDAR_DISCOVERED", "NOT_ELIGIBLE", "DECISION_GENERATED", "SETTLED"];

function pipelineEvent(i: number) {
  const lifecycle = LIFECYCLES[i % LIFECYCLES.length];
  return {
    calendar_event_id: 10_000 + i,
    symbol: `T${String(i).padStart(3, "0")}`,
    company_name: `Fixture Company ${i}`,
    market_cap: String(1_000_000_000 * (200 - i)),
    earnings_date: `2026-09-${String(3 + (i % 20)).padStart(2, "0")}`,
    earnings_timing: "amc",
    entry_timestamp: "2026-09-03T19:55:00Z",
    exit_timestamp: "2026-09-04T19:55:00Z",
    lifecycle_state: lifecycle,
    lifecycle_reason: lifecycle === "NOT_ELIGIBLE" ? "market cap below $10,000,000,000" : null,
    next_action: lifecycle === "CALENDAR_DISCOVERED" ? "Generate decision + capture entry" : null,
    next_action_at: lifecycle === "CALENDAR_DISCOVERED" ? "2026-09-03T19:55:00Z" : null,
    decision_snapshot_id: null,
    entry_capture_attempt_id: null,
    settlement_capture_attempt_id: null,
    timeline: [],
  };
}

async function mockOperations(page: import("@playwright/test").Page, eventCount: number) {
  const events = Array.from({ length: eventCount }, (_, i) => pipelineEvent(i + 1));
  await page.route("**/operations/summary", (route: Route) =>
    route.fulfill({
      json: {
        health: {
          ibkr: { state: "green", gateway_reachable: true, authenticated: true, connected: true, live_account: true, market_data_quality: "delayed", last_heartbeat_at: "2026-09-02T16:41:55Z", last_error: null, provider: "tws" },
          earnings_calendar: { state: "green", active_provider: "earningsapi", fallback_provider: "finnhub", last_successful_sync_at: "2026-09-02T00:00:00Z", events_received: eventCount, last_error: null, next_scheduled_sync_at: "2026-09-03T00:00:00Z" },
          ai_provider: { state: "green", provider: "deepseek", configured: true, last_successful_generation_at: "2026-09-01T19:55:10Z", last_error: null },
          scheduler: { state: "green", running: true, registered_job_count: 5, last_activity_at: "2026-09-02T16:41:03Z", next_activity_at: "2026-09-02T17:01:17Z" },
          database: { state: "green", backend_healthy: true, database_healthy: true, migration_head: "c1e5a7d93f20" },
        },
        today: { decision_window_et: "15:30", settlement_window_et: "15:30", deadline_et: "15:50", events_in_window: 1, business_eligible: 1, research_ready: 1, waiting_decision: 1, decisions_today: 0, ranked_today: 0, no_action_today: 0, entries_observed_today: 0, entries_failed_today: 0, deadline_skipped_today: 0, research_not_ready_today: 0, settlements_due_today: 0, settled_today: 0, settlements_failed_today: 0 },
        readiness: { window_days: 7, upcoming_events: 1, business_eligible: 1, company_resolved: 1, research_queued: 0, research_running: 0, research_ready: 1, research_failed: 0, ai_thesis_ready: 1, v4_decision_ready: 1, next_window_at: "2026-09-03T19:30:00Z", next_window_ready: 1, next_window_total: 1 },
        staleness: [],
        preflight: { checks: [], ready: true, blockers: [] },
        market_clock: { utc_now: "2026-09-02T16:45:00Z", new_york_now: "2026-09-02T12:45:00-04:00", zurich_now: "2026-09-02T18:45:00+02:00", market_session: "regular", next_automatic_action_job_id: "ibkr_gateway_healthcheck", next_automatic_action_at: "2026-09-02T17:01:17Z" },
      },
    }),
  );
  await page.route("**/operations/events", (route: Route) => route.fulfill({ json: { events } }));
  await page.route("**/operations/jobs", (route: Route) => route.fulfill({ json: { jobs: [] } }));
  await page.route("**/operations/failures", (route: Route) => route.fulfill({ json: { failures: [] } }));
  await page.route("**/operations/preparation-progress", (route: Route) =>
    route.fulfill({ json: { queue_depth: 0, completed: 0, failed: 0, worker_active: false, current_symbol: null, current_stage: null, step_index: null, step_total: null, attempt: null, heartbeat_seconds_ago: null, elapsed_seconds: null } }),
  );
  await page.route("**/v4/shadow/decisions", (route: Route) => route.fulfill({ json: { decisions: [] } }));
}

test.describe("Operations pipeline with a long list", () => {
  test("pages 120 events 25 at a time, keeps the state in the URL, and can show all", async ({ page }) => {
    await mockOperations(page, 120);
    await page.goto("/operations");

    const heading = page.getByRole("heading", { name: "V4 pipeline" });
    await expect(heading).toBeVisible();
    const card = page.locator(".card", { has: heading });
    const rows = card.locator("tbody > tr");

    await expect(rows).toHaveCount(25);
    await expect(page.getByTestId("pipeline-controls-range")).toHaveText("1–25 of 120");

    // Page through.
    await page.getByTestId("pipeline-pager").getByRole("button", { name: "Next →" }).click();
    await expect(page.getByTestId("pipeline-controls-range")).toHaveText("26–50 of 120");
    await expect(page).toHaveURL(/pipe_p=2/);
    await expect(rows.first()).toContainText("T026");

    // Search narrows to one row and resets to page 1.
    await page.getByTestId("pipeline-controls").getByRole("searchbox").fill("T077");
    await expect(rows).toHaveCount(1);
    await expect(page.getByTestId("pipeline-controls-range")).toHaveText("1 of 120");
    await expect(page).toHaveURL(/pipe_q=T077/);
    await page.getByTestId("pipeline-controls").getByRole("button", { name: "Clear" }).click();
    await expect(rows).toHaveCount(25);

    // Facet chips carry counts and filter.
    await page.getByTestId("pipeline-controls").getByRole("button", { name: /^NOT ELIGIBLE/ }).click();
    await expect(page.getByTestId("pipeline-controls-range")).toHaveText("1–25 of 30 (120 total)");
    await expect(card.locator("tbody > tr").first()).toContainText("market cap below");
    await page.getByTestId("pipeline-controls").getByRole("button", { name: /^All/ }).click();
    await expect(page.getByTestId("pipeline-controls-range")).toHaveText("1–25 of 120");
    await expect(page).not.toHaveURL(/pipe_f=/);

    // Sort by ticker puts T001 first (default order is the API's own).
    await expect(rows.first()).toContainText("T001");
    await page.getByTestId("pipeline-controls").getByRole("combobox", { name: "Sort" }).selectOption("cap");
    await expect(rows.first()).toContainText("T001");
    await page.getByTestId("pipeline-controls").getByRole("combobox", { name: "Sort" }).selectOption("ticker");
    await expect(page).toHaveURL(/pipe_s=ticker/);
    await expect(rows.first()).toContainText("T001");
    await expect(page.getByTestId("pipeline-controls-range")).toHaveText("1–25 of 120");

    // Everything stays reachable.
    await page.getByTestId("pipeline-pager").getByRole("button", { name: /Show all 120/ }).click();
    await expect(rows).toHaveCount(120);
    await expect(page.getByTestId("pipeline-controls-range")).toHaveText("120 of 120");
    await expect(page).toHaveURL(/pipe_n=all/);
  });

  test("a reloaded URL restores the same page and filter", async ({ page }) => {
    await mockOperations(page, 60);
    await page.goto("/operations?pipe_p=3&pipe_f=SETTLED&pipe_n=all");
    await expect(page.getByTestId("pipeline-controls-range")).toHaveText("15 of 60");
    const heading = page.getByRole("heading", { name: "V4 pipeline" });
    const card = page.locator(".card", { has: heading });
    await expect(card.locator("tbody > tr")).toHaveCount(15);
    await expect(card.getByRole("button", { name: /^SETTLED/ })).toHaveClass(/active/);
  });

  test("a short list shows no pager and no facet chips for a single state", async ({ page }) => {
    await mockOperations(page, 3);
    await page.goto("/operations");
    await expect(page.getByRole("heading", { name: "V4 pipeline" })).toBeVisible();
    await expect(page.getByTestId("pipeline-pager")).toHaveCount(0);
    await expect(page.getByTestId("pipeline-controls-range")).toHaveText("3 of 3");
  });

  test("the sticky outline lists the sections that are on the page", async ({ page }) => {
    await mockOperations(page, 5);
    await page.goto("/operations");
    const outline = page.getByTestId("page-outline");
    await expect(outline).toBeVisible();
    await expect(outline.getByRole("link", { name: "V4 pipeline" })).toBeVisible();
    await expect(outline.getByRole("link", { name: "V4 forward engine" })).toBeVisible();
    // Research preparation is not rendered for an idle queue, so it is not offered.
    await expect(outline.getByRole("link", { name: "Research prep" })).toHaveCount(0);
  });
});

test.describe("V4 decision picker with a long list", () => {
  test("searches and pages the decision list", async ({ page }) => {
    const decisions = Array.from({ length: 40 }, (_, i) => ({
      id: i + 1,
      ticker: `V${String(i + 1).padStart(2, "0")}`,
      company_name: `Shadow Fixture ${i + 1}`,
      earnings_calendar_event_id: 500 + i,
      legal_decision_window_at: "2026-09-02T19:30:00Z",
      status: i % 5 === 0 ? "NO_ACTION" : "RANKED",
      candidate_count: 14,
      rankable_candidate_count: 14,
      engine_version: "options-decision-engine-v4",
      decision_timing_policy_version: "v4-pre-earnings-1530et-v1",
    }));
    await page.route("**/v4/shadow/decisions", (route: Route) => route.fulfill({ json: { decisions } }));
    await page.route("**/v4/shadow/track-record**", (route: Route) => route.fulfill({ status: 404, json: { detail: "not mocked" } }));
    await page.goto("/v4-decision-lab");

    await expect(page.getByTestId("v4-decisions-controls-range")).toHaveText("1–25 of 40");
    await page.getByTestId("v4-decisions-controls").getByRole("button", { name: /^NO ACTION/ }).click();
    await expect(page.getByTestId("v4-decisions-controls-range")).toHaveText("8 of 40");
    await page.getByTestId("v4-decisions-controls").getByRole("button", { name: /^All/ }).click();
    await page.getByTestId("v4-decisions-controls").getByRole("searchbox").fill("Fixture 3");
    // "Shadow Fixture 3", 30-39 -> 11 rows
    await expect(page.getByTestId("v4-decisions-controls-range")).toHaveText("11 of 40");
  });
});
