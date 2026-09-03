import { test, expect, type Page, type Route } from "@playwright/test";

/**
 * Live Operations -- V4-only reset (2026-09-02). Every backend response is a
 * hand-labelled fixture matching backend/src/schemas/api.py's Operations*
 * response models via page.route(); this suite never talks to a backend and
 * never fabricates V4 evidence rows.
 */

const NOW = "2026-09-03T15:45:00Z";

function v4Health(overrides: Record<string, unknown> = {}) {
  return {
    state: "green",
    enabled: true,
    decisions_today: 0,
    ranked_today: 0,
    no_action_today: 0,
    failed_today: 0,
    entry_observations_failed_today: 0,
    settlements_due: 1,
    settlements_complete: 0,
    last_run_at: "2026-09-02T19:32:08Z",
    engine_version: "options-decision-engine-v4",
    decision_time_et: "15:30",
    settlement_time_et: "15:30",
    timing_policy_version: "v4-1530-entry-1530-t1-settlement-v2",
    note: "V4 forward test",
    ...overrides,
  };
}

function healthyHealth(overrides: Record<string, unknown> = {}) {
  return {
    ibkr: {
      state: "green",
      gateway_reachable: true,
      authenticated: true,
      connected: true,
      live_account: null,
      market_data_quality: "delayed",
      last_heartbeat_at: "2026-09-03T15:41:03Z",
      last_error: null,
      provider: "tws",
    },
    earnings_calendar: {
      state: "green",
      active_provider: "earningsapi",
      fallback_provider: "finnhub",
      last_successful_sync_at: "2026-09-03T00:00:12Z",
      events_received: 61,
      last_error: null,
      next_scheduled_sync_at: "2026-09-04T00:00:00Z",
    },
    ai_provider: {
      state: "green",
      provider: "deepseek",
      configured: true,
      last_successful_generation_at: "2026-09-02T19:32:08Z",
      last_error: null,
      decision_view_model: "deepseek-v4-pro",
      decision_view_thinking: "enabled",
      decision_view_reasoning_effort: "high",
      decision_view_max_tokens: 16384,
      decision_view_config_error: null,
    },
    scheduler: {
      state: "green",
      running: true,
      registered_job_count: 6,
      last_activity_at: "2026-09-03T15:41:03Z",
      next_activity_at: "2026-09-03T16:01:17Z",
    },
    database: { state: "green", backend_healthy: true, database_healthy: true, migration_head: "f4b6d8e0c2a3" },
    v4_shadow: v4Health(),
    ...overrides,
  };
}

function marketClock(overrides: Record<string, unknown> = {}) {
  return {
    utc_now: NOW,
    new_york_now: "2026-09-03T11:45:00-04:00",
    zurich_now: "2026-09-03T17:45:00+02:00",
    market_session: "regular",
    next_automatic_action_job_id: "v4_shadow_decision",
    next_automatic_action_at: "2026-09-03T19:30:00Z",
    settlement_window_tolerance_minutes: 5,
    ...overrides,
  };
}

function today(overrides: Record<string, unknown> = {}) {
  return {
    decision_window_et: "15:30",
    settlement_window_et: "15:30",
    deadline_et: "15:50",
    events_in_window: 3,
    business_eligible: 2,
    research_ready: 1,
    waiting_decision: 1,
    decisions_today: 0,
    ranked_today: 0,
    no_action_today: 0,
    entries_observed_today: 0,
    entries_failed_today: 0,
    deadline_skipped_today: 0,
    research_not_ready_today: 0,
    settlements_due_today: 1,
    settled_today: 0,
    settlements_failed_today: 0,
    ...overrides,
  };
}

function readiness(overrides: Record<string, unknown> = {}) {
  return {
    window_days: 7,
    upcoming_events: 12,
    business_eligible: 8,
    company_resolved: 7,
    research_queued: 2,
    research_running: 1,
    research_ready: 4,
    research_failed: 0,
    ai_thesis_ready: 4,
    v4_decision_ready: 4,
    next_window_at: "2026-09-03T19:30:00Z",
    next_window_ready: 1,
    next_window_total: 2,
    ...overrides,
  };
}

const EMPTY_PROGRESS = {
  queue_depth: 0,
  completed: 0,
  failed: 0,
  worker_active: false,
  current_symbol: null,
  current_stage: null,
  step_index: null,
  step_total: null,
  attempt: null,
  heartbeat_seconds_ago: null,
  elapsed_seconds: null,
};

async function mockOperations(
  page: Page,
  {
    health = healthyHealth(),
    preflight = { checks: [], ready: true, blockers: [] },
    events = [] as Record<string, unknown>[],
    jobs = [] as Record<string, unknown>[],
    failures = [] as Record<string, unknown>[],
    staleness = [] as Record<string, unknown>[],
    preparationProgress = EMPTY_PROGRESS as Record<string, unknown>,
  } = {},
) {
  await page.route("**/operations/summary", (route: Route) =>
    route.fulfill({ json: { health, today: today(), readiness: readiness(), preflight, market_clock: marketClock(), staleness, forward_window: { window_time_et: "15:30", priority: ["Due settlements", "New decision observations"], next_window_at: "2026-09-03T19:30:00Z", settlements_due: ["AVGO"], decisions_ready: ["CPRT", "DOCU", "GWRE", "IOT", "ZS"], decisions_not_ready: [], last_window_started_at: "2026-09-02T19:30:01Z", last_settlements_due: 0, last_settlements_settled: 0, last_settlements_failed: 0, last_settlements_window_missed: 0, last_settlement_lock_wait_ms_max: null, last_settlement_total_ms_max: null, last_decisions_ready: 1, last_deadline_skipped: 0, last_decision_lock_wait_ms: 0 } } }),
  );
  await page.route("**/operations/events", (route: Route) => route.fulfill({ json: { events } }));
  await page.route("**/operations/jobs", (route: Route) => route.fulfill({ json: { jobs } }));
  await page.route("**/operations/failures", (route: Route) => route.fulfill({ json: { failures } }));
  await page.route("**/operations/preparation-progress", (route: Route) => route.fulfill({ json: preparationProgress }));
  await page.route("**/v4/shadow/decisions", (route: Route) => route.fulfill({ json: { notice: "V4", decisions: [] } }));
}

const AVGO_EVENT = {
  calendar_event_id: 2887,
  symbol: "AVGO",
  company_name: "Broadcom Inc",
  market_cap: "1400000000000",
  earnings_date: "2026-09-02",
  earnings_timing: "amc",
  entry_timestamp: "2026-09-02T19:30:00Z",
  exit_timestamp: "2026-09-03T19:30:00Z",
  lifecycle_state: "WAITING_SETTLEMENT",
  lifecycle_reason: "6 of 6 configurations observed at entry",
  next_action: "Settle at 15:30 ET on the first post-earnings trading day",
  next_action_at: "2026-09-03T19:30:00Z",
  research_ready: true,
  shadow_decision_id: 5,
  decision_status: "RANKED",
  entries_observed: 6,
  entries_failed: 0,
  settlements_settled: 0,
  settlements_failed: 0,
  timeline: [
    { label: "Earnings event synced", at: "2026-08-22T00:04:16Z", status: "done", detail: "Source: earningsapi" },
    { label: "Research ready", at: "2026-09-01T02:11:00Z", status: "done", detail: null },
    { label: "V4 decision", at: "2026-09-02T19:32:08Z", status: "done", detail: "RANKED" },
    { label: "Entry observed", at: "2026-09-02T19:32:20Z", status: "done", detail: "6 configurations" },
    { label: "Settlement", at: null, status: "pending", detail: "Scheduled: 2026-09-03T19:30:00Z" },
  ],
};

const SNOW_EVENT = {
  ...AVGO_EVENT,
  calendar_event_id: 2901,
  symbol: "SNOW",
  company_name: "Snowflake Inc",
  earnings_date: "2026-09-03",
  entry_timestamp: "2026-09-03T19:30:00Z",
  exit_timestamp: "2026-09-04T19:30:00Z",
  lifecycle_state: "RESEARCH_READY",
  lifecycle_reason: null,
  next_action: "V4 decision at 15:30 ET",
  next_action_at: "2026-09-03T19:30:00Z",
  shadow_decision_id: null,
  decision_status: null,
  entries_observed: 0,
  timeline: [],
};

const XYZ_INELIGIBLE = {
  ...SNOW_EVENT,
  calendar_event_id: 4001,
  symbol: "XYZ",
  company_name: "XYZ Test Co",
  lifecycle_state: "BUSINESS_INELIGIBLE",
  lifecycle_reason: "market cap below $10,000,000,000",
  next_action: null,
  next_action_at: null,
  research_ready: false,
};

const HPE_UNRESOLVED = {
  ...SNOW_EVENT,
  calendar_event_id: 4002,
  symbol: "HPE",
  company_name: "Hewlett Packard Enterprise",
  lifecycle_state: "COMPANY_RESOLUTION_FAILED",
  lifecycle_reason: "no longer a supported symbol: HPE not found in EDGAR",
  research_ready: false,
};

const CIEN_FAILED = {
  ...SNOW_EVENT,
  calendar_event_id: 4003,
  symbol: "CIEN",
  company_name: "Ciena Corp",
  lifecycle_state: "RESEARCH_FAILED",
  lifecycle_reason: "SEC EDGAR outage",
  research_ready: false,
};

const NOACTION_EVENT = {
  ...AVGO_EVENT,
  calendar_event_id: 4004,
  symbol: "DOCU",
  company_name: "DocuSign",
  lifecycle_state: "NO_ACTION",
  lifecycle_reason: "no candidate fit any configuration",
  next_action: null,
  next_action_at: null,
  entries_observed: 0,
};

const HEALTHCHECK_JOB = {
  job_id: "ibkr_gateway_healthcheck",
  enabled: true,
  last_run_at: "2026-09-03T15:41:03Z",
  last_run_status: "success",
  duration_ms: 105,
  items_evaluated: null,
  items_succeeded: null,
  items_failed: null,
  next_run_time: "2026-09-03T16:01:17Z",
  last_error: null,
};

const V4_WINDOW_JOB = { ...HEALTHCHECK_JOB, job_id: "v4_forward_window", last_run_at: null, last_run_status: null, duration_ms: null, next_run_time: "2026-09-03T19:30:00Z" };
const V4_DECISION_JOB = { ...V4_WINDOW_JOB, job_id: "v4_shadow_decision" };
const V4_SETTLEMENT_JOB = { ...V4_WINDOW_JOB, job_id: "v4_shadow_settlement" };
const PREP_JOB = { ...HEALTHCHECK_JOB, job_id: "earnings_research_preparation", last_run_at: "2026-09-03T01:00:04Z", next_run_time: "2026-09-04T01:00:00Z" };

test.describe("Live Operations (V4-only)", () => {
  test("renders a healthy V4 system with READY preflight, today's window and readiness KPIs", async ({ page }) => {
    await mockOperations(page, { jobs: [HEALTHCHECK_JOB, V4_WINDOW_JOB, V4_DECISION_JOB, V4_SETTLEMENT_JOB, PREP_JOB] });
    await page.goto("/operations");
    await expect(page.getByRole("heading", { name: "Live Operations" })).toBeVisible();
    await expect(page.getByText("READY FOR TODAY'S FORWARD TEST")).toBeVisible();
    await expect(page.getByText("GREEN").first()).toBeVisible();
    await expect(page.getByTestId("ops-today")).toContainText("15:30 ET");
    await expect(page.getByTestId("ops-today")).toContainText("deadline 15:50 ET");
    await expect(page.getByTestId("ops-readiness")).toContainText("V4 decision ready");
    await expect(page.getByTestId("ops-readiness")).toContainText("1 / 2");
    await expect(page.getByTestId("operations-v4")).toContainText("v4-1530-entry-1530-t1-settlement-v2");
    await expect(page.getByTestId("operations-v4")).toContainText("no brokerage orders");
    // The 15:30 forward window states its execution order and previews real symbols.
    const fw = page.getByTestId("ops-forward-window");
    await expect(fw).toContainText("V4 Forward Window — 15:30 ET");
    await expect(fw).toContainText("1. Due settlements · 2. New decision observations");
    await expect(fw).toContainText("AVGO");
    await expect(fw).toContainText("CPRT, DOCU, GWRE, IOT, ZS");
    // Nothing from the retired control engine survives.
    await expect(page.locator("body")).not.toContainText("V3");
    await expect(page.locator("body")).not.toContainText("Legacy");
    await expect(page.locator("body")).not.toContainText("15:55");
  });

  test("System health shows the TWS provider without a false paper/live account claim", async ({ page }) => {
    await mockOperations(page);
    await page.goto("/operations");
    const systemCard = page.getByTestId("system-health");
    await expect(systemCard.getByText("TWS · delayed")).toBeVisible();
    await expect(systemCard.getByText("DeepSeek · V4 view: deepseek-v4-pro · thinking high")).toBeVisible();
    await expect(systemCard.getByText("Live account", { exact: true })).not.toBeVisible();
    await expect(systemCard.getByText("Paper account", { exact: true })).not.toBeVisible();
  });

  test("renders a critical banner and NOT READY when the scheduler is down", async ({ page }) => {
    await mockOperations(page, {
      health: healthyHealth({ scheduler: { state: "red", running: false, registered_job_count: 0, last_activity_at: null, next_activity_at: null } }),
      preflight: { checks: [], ready: false, blockers: ["scheduler not running"] },
    });
    await page.goto("/operations");
    await expect(page.getByText("CRITICAL: Scheduler not running.")).toBeVisible();
    await expect(page.getByText(/NOT READY — scheduler not running/)).toBeVisible();
    await expect(page.getByText("RED").first()).toBeVisible();
  });

  test("surfaces STALE and MISSED RUN staleness from the backend prominently and per job", async ({ page }) => {
    await mockOperations(page, {
      jobs: [PREP_JOB, V4_DECISION_JOB],
      staleness: [
        { job_id: "earnings_research_preparation", state: "stale", last_expected_at: "2026-09-02T01:00:00Z", last_actual_at: "2026-08-25T01:00:04Z", next_run_time: null, next_run_at: "2026-09-04T01:00:00Z", detail: "last successful run 9 days ago" },
        { job_id: "v4_shadow_decision", state: "missed", last_expected_at: "2026-09-02T19:30:00Z", last_actual_at: null, next_run_at: "2026-09-03T19:30:00Z", detail: "no run recorded for the 2026-09-02 15:30 ET window" },
      ],
    });
    await page.goto("/operations");
    await expect(page.getByText(/CRITICAL: MISSED RUN — V4 Decision phase \(15:30 ET\)/)).toBeVisible();
    await expect(page.getByText(/WARNING: STALE — Research Preparation \(nightly\)/)).toBeVisible();
    const jobs = page.getByTestId("ops-jobs");
    await expect(jobs.getByText("MISSED RUN")).toBeVisible();
    await expect(jobs.getByText("STALE")).toBeVisible();
  });

  test("shows live preparation progress while a worker is actively claimed", async ({ page }) => {
    await mockOperations(page, {
      preparationProgress: { queue_depth: 3, completed: 9, failed: 0, worker_active: true, current_symbol: "SNPS", current_stage: "SEC filings", step_index: 5, step_total: 8, attempt: 1, heartbeat_seconds_ago: 4, elapsed_seconds: 61 },
    });
    await page.goto("/operations");
    const card = page.getByTestId("ops-research-prep");
    await expect(card.getByText("SNPS")).toBeVisible();
    await expect(card.getByText("SEC filings (5 / 8)")).toBeVisible();
    await expect(card.getByText("3 pending")).toBeVisible();
  });

  test("hides preparation progress when the queue is entirely empty", async ({ page }) => {
    await mockOperations(page);
    await page.goto("/operations");
    await expect(page.getByRole("heading", { name: "Live Operations" })).toBeVisible();
    await expect(page.getByTestId("ops-research-prep")).toHaveCount(0);
  });

  test("scheduler job rows show V4 labels, real status and next run — and no retired V3 job", async ({ page }) => {
    await mockOperations(page, { jobs: [HEALTHCHECK_JOB, V4_WINDOW_JOB, V4_DECISION_JOB, V4_SETTLEMENT_JOB, PREP_JOB] });
    await page.goto("/operations");
    const jobsCard = page.getByTestId("ops-jobs");
    await expect(jobsCard.getByText("IBKR Provider Healthcheck")).toBeVisible();
    await expect(jobsCard.getByText("READY").first()).toBeVisible();
    await expect(jobsCard.getByText("V4 Forward Window (15:30 ET: settlements, then decisions)")).toBeVisible();
    await expect(jobsCard.getByText("V4 Decision phase (15:30 ET)")).toBeVisible();
    await expect(jobsCard.getByText("V4 Settlement phase (15:30 ET, T+1)")).toBeVisible();
    await expect(jobsCard.getByText("NO RUNS YET").first()).toBeVisible();
    await expect(jobsCard.getByText("Decision + Entry Capture")).toHaveCount(0);
    await expect(jobsCard.getByText("Exit Capture")).toHaveCount(0);
  });

  test("pipeline rows show the V4 states with human labels, including unresolved and failed research", async ({ page }) => {
    await mockOperations(page, { events: [AVGO_EVENT, SNOW_EVENT, XYZ_INELIGIBLE, HPE_UNRESOLVED, CIEN_FAILED] });
    await page.goto("/operations");
    const body = page.getByTestId("ops-pipeline").locator("tbody");
    await expect(body.getByText("WAITING SETTLEMENT")).toBeVisible();
    await expect(body.getByText("READY FOR V4 DECISION")).toBeVisible();
    await expect(body.getByText("NOT ELIGIBLE")).toBeVisible();
    await expect(body.getByText("COMPANY UNRESOLVED")).toBeVisible();
    await expect(body.getByText("RESEARCH FAILED")).toBeVisible();
    await expect(body.getByText("SEC EDGAR outage")).toBeVisible();
    await expect(body.getByRole("link", { name: "decision →" })).toHaveAttribute("href", "/v4-decision-lab/5");
  });

  test("a no-action decision shows NO ACTION, never ENTRY FAILED", async ({ page }) => {
    await mockOperations(page, { events: [NOACTION_EVENT] });
    await page.goto("/operations");
    const body = page.getByTestId("ops-pipeline").locator("tbody");
    await expect(body.getByText("NO ACTION", { exact: true })).toBeVisible();
    await expect(body.getByText("ENTRY FAILED")).toHaveCount(0);
  });

  test("expanding a pipeline row reveals the settlement schedule and timeline", async ({ page }) => {
    await mockOperations(page, { events: [AVGO_EVENT] });
    await page.goto("/operations");
    await page.getByTestId("ops-pipeline").locator("tbody tr").first().click();
    const pipeline = page.getByTestId("ops-pipeline");
    await expect(pipeline.getByText("Settlement", { exact: true })).toBeVisible();
    await expect(pipeline.getByText("6 configurations", { exact: true })).toBeVisible();
  });

  test("has no force-decision/force-entry/force-settlement/override controls anywhere", async ({ page }) => {
    await mockOperations(page, { events: [AVGO_EVENT, SNOW_EVENT], jobs: [V4_DECISION_JOB, V4_SETTLEMENT_JOB] });
    await page.goto("/operations");
    await expect(page.getByRole("heading", { name: "Live Operations" })).toBeVisible();
    for (const name of [/force/i, /run now/i, /settle now/i, /override/i, /retry entry/i, /place order/i]) {
      await expect(page.getByRole("button", { name })).toHaveCount(0);
    }
  });

  test("clicking a ticker navigates to the company workspace", async ({ page }) => {
    await mockOperations(page, { events: [SNOW_EVENT] });
    await page.route("**/research/SNOW/overview", (route: Route) => route.fulfill({ status: 404, json: { error: "not mocked" } }));
    await page.goto("/operations");
    await page.getByTestId("ops-pipeline").getByRole("link", { name: "SNOW" }).click();
    await expect(page).toHaveURL(/\/company\/SNOW$/);
  });

  test("navigation is V4-only: no Legacy/Control section and no V3 links", async ({ page }) => {
    await mockOperations(page);
    await page.goto("/operations");
    const nav = page.locator(".sidebar");
    await expect(nav.getByText("Live Operations")).toBeVisible();
    await expect(nav.getByText("V4 Decision Lab")).toBeVisible();
    await expect(nav.getByText("V4 Forward Track Record")).toBeVisible();
    await expect(nav.getByText("Legacy / Control")).toHaveCount(0);
    await expect(nav.getByText("V3 Control Track Record")).toHaveCount(0);
    await expect(nav.getByText("Same-Event Comparison")).toHaveCount(0);
    await expect(nav.getByText("AI Decision Journal")).toHaveCount(0);
    await expect(nav.getByText("Strategy Lab")).toHaveCount(0);
  });
});
