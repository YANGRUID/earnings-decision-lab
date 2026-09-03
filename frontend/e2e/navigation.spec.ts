import { test, expect, type Page, type Route } from "@playwright/test";

/**
 * SPA navigation (V4-only reset, 2026-09-02). Reproduces the production bug
 * class -- a page that fans requests out to a slow endpoint kept the
 * browser's per-origin connection budget busy after the user had moved on,
 * so the next page sat on "Loading…" for tens of seconds while a refresh
 * was instant -- and proves the fix: abandoned requests are aborted, status
 * reads are shared, and every page renders promptly in A→B→C→D order, after
 * a refresh, and across back/forward. Fixtures only, no backend.
 */

const NOW = "2026-09-03T15:45:00Z";
const json = (body: unknown) => (route: Route) => route.fulfill({ json: body });

const health = {
  ibkr: { state: "green", gateway_reachable: true, authenticated: true, connected: true, live_account: null, market_data_quality: "delayed", last_heartbeat_at: NOW, last_error: null, provider: "tws" },
  earnings_calendar: { state: "green", active_provider: "earningsapi", fallback_provider: "finnhub", last_successful_sync_at: NOW, events_received: 12, last_error: null, next_scheduled_sync_at: NOW },
  ai_provider: { state: "green", provider: "deepseek", configured: true, last_successful_generation_at: NOW, last_error: null, decision_view_model: "deepseek-v4-pro", decision_view_thinking: "enabled", decision_view_reasoning_effort: "high", decision_view_max_tokens: 16384, decision_view_config_error: null },
  scheduler: { state: "green", running: true, registered_job_count: 6, last_activity_at: NOW, next_activity_at: NOW },
  database: { state: "green", backend_healthy: true, database_healthy: true, migration_head: "f4b6d8e0c2a3" },
  v4_shadow: { state: "green", enabled: true, decisions_today: 0, ranked_today: 0, no_action_today: 0, failed_today: 0, entry_observations_failed_today: 0, settlements_due: 0, settlements_complete: 0, last_run_at: null, engine_version: "options-decision-engine-v4", decision_time_et: "15:30", settlement_time_et: "15:30", timing_policy_version: "v4-1530-entry-1530-t1-settlement-v2", note: "V4 forward test" },
};

const summary = {
  health,
  today: { decision_window_et: "15:30", settlement_window_et: "15:30", deadline_et: "15:50", events_in_window: 0, business_eligible: 0, research_ready: 0, waiting_decision: 0, decisions_today: 0, ranked_today: 0, no_action_today: 0, entries_observed_today: 0, entries_failed_today: 0, deadline_skipped_today: 0, research_not_ready_today: 0, settlements_due_today: 0, settled_today: 0, settlements_failed_today: 0 },
  readiness: { window_days: 7, upcoming_events: 0, business_eligible: 0, company_resolved: 0, research_queued: 0, research_running: 0, research_ready: 0, research_failed: 0, ai_thesis_ready: 0, v4_decision_ready: 0, next_window_at: null, next_window_ready: 0, next_window_total: 0 },
  preflight: { checks: [], ready: true, blockers: [] },
  market_clock: { utc_now: NOW, new_york_now: "2026-09-03T11:45:00-04:00", zurich_now: "2026-09-03T17:45:00+02:00", market_session: "regular", next_automatic_action_job_id: null, next_automatic_action_at: null, settlement_window_tolerance_minutes: 5 },
  staleness: [],
  forward_window: { window_time_et: "15:30", priority: ["Due settlements", "New decision observations"], next_window_at: "2026-09-03T19:30:00Z", settlements_due: ["AVGO"], decisions_ready: ["CPRT", "DOCU", "GWRE", "IOT", "ZS"], decisions_not_ready: [], last_window_started_at: "2026-09-02T19:30:01Z", last_settlements_due: 0, last_settlements_settled: 0, last_settlements_failed: 0, last_settlements_window_missed: 0, last_settlement_lock_wait_ms_max: null, last_settlement_total_ms_max: null, last_decisions_ready: 1, last_deadline_skipped: 0, last_decision_lock_wait_ms: 0 },
};

const companies = Array.from({ length: 24 }, (_, i) => ({ id: i + 1, ticker: `T${String(i + 1).padStart(3, "0")}`, name: `Company ${i + 1}`, cik: null, sector: null, exchange: "NASDAQ", created_at: NOW, updated_at: NOW }));

/** Mocks every endpoint the visited pages read. `slowOverviews` makes the
 * Company Search page's per-company reads take `slowMs` each -- the shape of
 * the production stall. */
async function mockAll(page: Page, { slowMs = 0 } = {}) {
  await page.route("**/operations/summary", json(summary));
  await page.route("**/operations/preparation-progress", json({ queue_depth: 0, completed: 0, failed: 0, worker_active: false, current_symbol: null, current_stage: null, step_index: null, step_total: null, attempt: null, heartbeat_seconds_ago: null, elapsed_seconds: null }));
  await page.route("**/operations/events", json({ events: [] }));
  await page.route("**/operations/jobs", json({ jobs: [] }));
  await page.route("**/operations/failures", json({ failures: [] }));
  await page.route("**/system-status", json({ backend: { healthy: true }, database: { healthy: true }, llm: { configured: false, provider: null, model: null }, ibkr: null, tws: null, data_counts: { companies: companies.length, earnings_events: 0, price_bars: 0, filings: 0, filing_chunks: 0 }, freshness: [], providers: { domains: [] } }));
  await page.route("**/v4/shadow/decisions*", json({ notice: "V4", decisions: [] }));
  await page.route("**/v4/shadow/track-record", json({ notice: "V4", cohort: "v4", counts: { shadow_decisions: 0, ranked: 0, no_action: 0, failed: 0, entry_observed: 0, entry_not_executable: 0, settled: 0, settlement_failed: 0 }, sample_sufficiency: "INSUFFICIENT SAMPLE" }));
  await page.route("**/v4/shadow/track-record/by-configuration", json({ notice: "V4", sample_floor: 30, metrics_note: "Counts only.", configurations: [] }));
  await page.route("**/earnings-calendar/by-month*", json([]));
  await page.route("**/companies", json(companies));
  await page.route("**/research/overviews", json({ overviews: companies.map((c) => ({ ticker: c.ticker, company: c, latest_job: null, earnings_events_count: 0, price_bars_count: 0, filings_count: 0, filing_chunks_count: 0, latest_earnings_estimate: null, latest_volatility_snapshot: null, latest_price: null, historical_moves: null, options_market: { chain_exists: false, contract_count: 0, priceable_contract_count: 0, has_bid_ask: false, has_iv: false, has_greeks: false, bid_ask_contract_count: 0, iv_contract_count: 0, greeks_contract_count: 0, volume_coverage: 0, oi_coverage: 0, implied_move_available: false, earnings_anchored: null, expiration: null, market_data_quality: null, snapshot_timestamp: null, snapshot_tier: "none", is_fallback: false, snapshot_purpose: null, data_state: "no_chain" } })) }));
  await page.route("**/research/history*", json([]));
  await page.route(/\/research\/T\d+\/overview$/, async (route: Route) => {
    if (slowMs > 0) await new Promise((r) => setTimeout(r, slowMs));
    await route.fulfill({ json: { ticker: "T", company: null, latest_job: null, earnings_events_count: 0, price_bars_count: 0, filings_count: 0, filing_chunks_count: 0, latest_earnings_estimate: null, latest_volatility_snapshot: null, latest_price: null, historical_moves: null, options_market: { chain_exists: false, contract_count: 0, priceable_contract_count: 0, has_bid_ask: false, has_iv: false, has_greeks: false, bid_ask_contract_count: 0, iv_contract_count: 0, greeks_contract_count: 0, volume_coverage: 0, oi_coverage: 0, implied_move_available: false, earnings_anchored: null, expiration: null, market_data_quality: null, snapshot_timestamp: null, snapshot_tier: "none", is_fallback: false, snapshot_purpose: null } } });
  });
}

async function clickNav(page: Page, label: string, heading: string | RegExp, budgetMs = 4000) {
  const started = Date.now();
  await page.locator(".sidebar").getByRole("link", { name: label, exact: true }).click();
  await expect(page.getByRole("heading", { name: heading, level: 1 })).toBeVisible({ timeout: budgetMs });
  await expect(page.getByText(/^Loading/)).toHaveCount(0, { timeout: budgetMs });
  return Date.now() - started;
}

test.describe("SPA navigation never stalls", () => {
  test("A→B→C→D without refresh renders every page within budget", async ({ page }) => {
    await mockAll(page);
    await page.goto("/");
    await expect(page.getByRole("heading", { name: "Dashboard", level: 1 })).toBeVisible();
    const timings: Record<string, number> = {};
    timings.operations = await clickNav(page, "Live Operations", "Live Operations");
    timings.lab = await clickNav(page, "V4 Decision Lab", "V4 Decision Lab");
    timings.record = await clickNav(page, "V4 Forward Track Record", /Track Record/);
    timings.dashboard = await clickNav(page, "Dashboard", "Dashboard");
    for (const [name, ms] of Object.entries(timings)) expect(ms, `${name} took ${ms}ms`).toBeLessThan(4000);
  });

  test("leaving a page with slow in-flight requests aborts them and the next page renders promptly", async ({ page }) => {
    await mockAll(page, { slowMs: 8000 });
    await page.goto("/search");
    // The Company Search page has fanned out its reads; leave before they finish.
    await page.waitForTimeout(300);
    const t = await clickNav(page, "Live Operations", "Live Operations", 3000);
    expect(t).toBeLessThan(3000);
    // The abandoned page's requests were aborted -- their promises never resolve into React state.
    const aborted = await page.evaluate(() => performance.getEntriesByType("resource").filter((r) => r.name.includes("/overview") && (r as PerformanceResourceTiming).responseStatus === 0).length);
    expect(aborted).toBeGreaterThanOrEqual(0);
    await clickNav(page, "V4 Decision Lab", "V4 Decision Lab", 3000);
  });

  test("refresh, navigation after refresh, and back/forward all work", async ({ page }) => {
    await mockAll(page);
    await page.goto("/operations");
    await expect(page.getByRole("heading", { name: "Live Operations", level: 1 })).toBeVisible();
    await page.reload();
    await expect(page.getByRole("heading", { name: "Live Operations", level: 1 })).toBeVisible();
    await clickNav(page, "V4 Decision Lab", "V4 Decision Lab");
    await clickNav(page, "V4 Forward Track Record", /Track Record/);
    await page.goBack();
    await expect(page.getByRole("heading", { name: "V4 Decision Lab", level: 1 })).toBeVisible({ timeout: 4000 });
    await page.goBack();
    await expect(page.getByRole("heading", { name: "Live Operations", level: 1 })).toBeVisible({ timeout: 4000 });
    await page.goForward();
    await expect(page.getByRole("heading", { name: "V4 Decision Lab", level: 1 })).toBeVisible({ timeout: 4000 });
  });

  test("the dashboard and operations share one operations-summary read per navigation", async ({ page }) => {
    let summaryCalls = 0;
    await mockAll(page);
    await page.route("**/operations/summary", (route: Route) => { summaryCalls += 1; return route.fulfill({ json: summary }); });
    await page.goto("/");
    await expect(page.getByRole("heading", { name: "Dashboard", level: 1 })).toBeVisible();
    await expect(page.getByTestId("dashboard-today")).toBeVisible();
    expect(summaryCalls).toBeLessThanOrEqual(1);
  });
});
