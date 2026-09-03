import { test, expect, type Page, type Route } from "@playwright/test";

/**
 * Dashboard earnings calendar (v4.0.x): clicking a ticker, a day number or
 * the "+N more" overflow opens a full day table under the grid -- every
 * company reporting that day, with its V4 pipeline state where the event
 * lies inside the 15:30 ET window -- and each ticker links to its company
 * workspace. Fixtures only, no backend.
 */

const NOW = "2026-09-03T15:45:00Z";
const json = (body: unknown) => (route: Route) => route.fulfill({ json: body });

// A day in the CURRENT month (the grid opens on today's month).
const today = new Date();
const DAY = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, "0")}-15`;
const OTHER = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, "0")}-16`;

const companies = [
  ["AVGO", "Broadcom Inc", "1400000000000", "amc"],
  ["ORCL", "Oracle Corp", "410000000000", "amc"],
  ["CPRT", "Copart Inc", "30800000000", "amc"],
  ["DOCU", "DocuSign", "11900000000", "amc"],
  ["CASY", "Caseys General Stores", "13500000000", "bmo"],
  ["SAIL", "SailPoint Inc", "10900000000", "bmo"],
] as const;

const monthEvents = [
  ...companies.map(([symbol, name, cap, time], i) => ({
    id: 100 + i, symbol, company_name: name, logo_url: null, earnings_date: DAY, earnings_time: time,
    eps_estimate: i % 2 === 0 ? "1.25" : null, revenue_estimate: i % 2 === 0 ? "15000000000" : null,
    market_cap: cap, source: "earningsapi",
  })),
  { id: 200, symbol: "ZS", company_name: "Zscaler Inc", logo_url: null, earnings_date: OTHER, earnings_time: "amc", eps_estimate: null, revenue_estimate: null, market_cap: "24000000000", source: "earningsapi" },
];

const pipelineRow = {
  calendar_event_id: 102, symbol: "CPRT", company_name: "Copart Inc", market_cap: "30800000000",
  earnings_date: DAY, earnings_timing: "amc", entry_timestamp: `${DAY}T19:30:00Z`, exit_timestamp: `${DAY}T19:30:00Z`,
  lifecycle_state: "WAITING_DECISION", lifecycle_reason: null, next_action: "V4 decision at 15:30 ET", next_action_at: `${DAY}T19:30:00Z`,
  research_ready: true, shadow_decision_id: null, decision_status: null, entries_observed: 0, entries_failed: 0,
  settlements_settled: 0, settlements_failed: 0, timeline: [],
};

const health = {
  ibkr: { state: "green", gateway_reachable: true, authenticated: true, connected: true, live_account: null, market_data_quality: "delayed", last_heartbeat_at: NOW, last_error: null, provider: "tws" },
  earnings_calendar: { state: "green", active_provider: "earningsapi", fallback_provider: "finnhub", last_successful_sync_at: NOW, events_received: 7, last_error: null, next_scheduled_sync_at: NOW },
  ai_provider: { state: "green", provider: "deepseek", configured: true, last_successful_generation_at: NOW, last_error: null, decision_view_model: "deepseek-v4-pro", decision_view_thinking: "enabled", decision_view_reasoning_effort: "high", decision_view_max_tokens: 16384, decision_view_config_error: null },
  scheduler: { state: "green", running: true, registered_job_count: 5, last_activity_at: NOW, next_activity_at: NOW },
  database: { state: "green", backend_healthy: true, database_healthy: true, migration_head: "b7d9f1a3c5e7" },
  v4_shadow: { state: "green", enabled: true, decisions_today: 0, ranked_today: 0, no_action_today: 0, failed_today: 0, entry_observations_failed_today: 0, settlements_due: 0, settlements_complete: 0, last_run_at: null, engine_version: "options-decision-engine-v4", decision_time_et: "15:30", settlement_time_et: "15:30", timing_policy_version: "v4-1530-entry-1530-t1-settlement-v2", note: "V4 forward test" },
};

async function mockDashboard(page: Page) {
  await page.route("**/operations/summary", json({
    health,
    today: { decision_window_et: "15:30", settlement_window_et: "15:30", deadline_et: "15:50", events_in_window: 0, business_eligible: 0, research_ready: 0, waiting_decision: 0, decisions_today: 0, ranked_today: 0, no_action_today: 0, entries_observed_today: 0, entries_failed_today: 0, deadline_skipped_today: 0, research_not_ready_today: 0, settlements_due_today: 0, settled_today: 0, settlements_failed_today: 0 },
    readiness: { window_days: 7, upcoming_events: 0, business_eligible: 0, company_resolved: 0, research_queued: 0, research_running: 0, research_ready: 0, research_failed: 0, ai_thesis_ready: 0, v4_decision_ready: 0, next_window_at: null, next_window_ready: 0, next_window_total: 0 },
    preflight: { checks: [], ready: true, blockers: [] },
    market_clock: { utc_now: NOW, new_york_now: "2026-09-03T11:45:00-04:00", zurich_now: "2026-09-03T17:45:00+02:00", market_session: "regular", next_automatic_action_job_id: null, next_automatic_action_at: null, settlement_window_tolerance_minutes: 5 },
    staleness: [],
    forward_window: { window_time_et: "15:30", priority: ["Due settlements", "New decision observations"], next_window_at: null, settlements_due: [], decisions_ready: [], decisions_not_ready: [], last_window_started_at: null, last_settlements_due: 0, last_settlements_settled: 0, last_settlements_failed: 0, last_settlements_window_missed: 0, last_settlement_lock_wait_ms_max: null, last_settlement_total_ms_max: null, last_decisions_ready: 0, last_deadline_skipped: 0, last_decision_lock_wait_ms: null },
  }));
  await page.route("**/operations/preparation-progress", json({ queue_depth: 0, completed: 0, failed: 0, worker_active: false, current_symbol: null, current_stage: null, step_index: null, step_total: null, attempt: null, heartbeat_seconds_ago: null, elapsed_seconds: null }));
  await page.route("**/operations/events*", json({ events: [pipelineRow] }));
  await page.route("**/v4/shadow/decisions*", json({ notice: "V4", decisions: [] }));
  await page.route("**/v4/shadow/track-record/by-configuration", json({ notice: "V4", sample_floor: 30, metrics_note: "Counts only.", configurations: [] }));
  await page.route("**/earnings-calendar/by-month*", json(monthEvents));
  await page.route("**/research/*/overview", json({ ticker: "X", company: null, latest_job: null, earnings_events_count: 0, price_bars_count: 0, filings_count: 0, filing_chunks_count: 0, latest_earnings_estimate: null, latest_volatility_snapshot: null, latest_price: null, historical_moves: null, options_market: { chain_exists: false, contract_count: 0, priceable_contract_count: 0, has_bid_ask: false, has_iv: false, has_greeks: false, bid_ask_contract_count: 0, iv_contract_count: 0, greeks_contract_count: 0, volume_coverage: 0, oi_coverage: 0, implied_move_available: false, earnings_anchored: null, expiration: null, market_data_quality: null, snapshot_timestamp: null, snapshot_tier: "none", is_fallback: false, snapshot_purpose: null, data_state: "no_chain" } }));
}

test.describe("Dashboard earnings calendar", () => {
  test("the overflow opens the full day table with every company", async ({ page }) => {
    await mockDashboard(page);
    await page.goto("/");
    const cell = page.locator(`.calendar-cell[data-date="${DAY}"]`);
    await expect(cell).toContainText("+2 more");
    await cell.getByRole("button", { name: "+2 more" }).click();
    const table = page.getByTestId("calendar-day-table");
    await expect(table).toBeVisible();
    await expect(table).toContainText("6 companies");
    await expect(table.locator("tbody tr")).toHaveCount(6);
    // Largest market cap first, and the pipeline state where one exists.
    await expect(table.locator("tbody tr").first()).toContainText("AVGO");
    await expect(table.locator("tr[data-symbol='CPRT']")).toContainText("WAITING DECISION");
    await expect(table.locator("tr[data-symbol='SAIL']")).toContainText("outside the V4 window");
    await table.getByRole("button", { name: "Close day view" }).click();
    await expect(page.getByTestId("calendar-day-table")).toHaveCount(0);
  });

  test("clicking a ticker opens its day with that row highlighted and links to the workspace", async ({ page }) => {
    await mockDashboard(page);
    await page.goto("/");
    await page.locator(`.calendar-cell[data-date="${DAY}"]`).getByRole("button", { name: /ORCL/ }).click();
    const table = page.getByTestId("calendar-day-table");
    await expect(table).toBeVisible();
    await expect(table.locator("tr.calendar-day-row--active")).toHaveAttribute("data-symbol", "ORCL");
    await expect(page.locator(`.calendar-cell[data-date="${DAY}"]`)).toHaveClass(/calendar-cell-selected/);
    await table.locator("tr[data-symbol='ORCL']").getByRole("link", { name: "ORCL" }).click();
    await expect(page).toHaveURL(/\/company\/ORCL$/);
  });

  test("a day number opens the same table and months navigate without stale selection", async ({ page }) => {
    await mockDashboard(page);
    await page.goto("/");
    await page.locator(`.calendar-cell[data-date="${OTHER}"]`).getByRole("button", { name: "16" }).click();
    await expect(page.getByTestId("calendar-day-table")).toContainText("1 company");
    await expect(page.getByTestId("calendar-day-table")).toContainText("ZS");
    await page.getByRole("button", { name: "Next month" }).click();
    await expect(page.getByTestId("calendar-day-table")).toHaveCount(0);
  });
});
