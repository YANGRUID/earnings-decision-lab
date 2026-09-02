import { test, expect, type Route } from "@playwright/test";

/**
 * Pre-live hardening (2026-08-25) Section 8 -- narrow smoke coverage for
 * the Live Operations Monitor (src/pages/Operations.tsx). Every backend
 * response here is a deliberately hand-labeled fixture matching the real
 * schemas (backend/src/schemas/api.py's Operations* response models) via
 * page.route() -- this suite never talks to a real backend/database, and
 * never fabricates a real DecisionSnapshot/EntrySnapshot/SettlementSnapshot.
 * See playwright.operations.config.ts for why this is its own, backend-
 * free config, same pattern as playwright.ibkr.config.ts.
 */

function healthyHealth(overrides: Record<string, unknown> = {}) {
  return {
    ibkr: {
      state: "green",
      gateway_reachable: true,
      authenticated: true,
      connected: true,
      live_account: true,
      market_data_quality: "delayed",
      last_heartbeat_at: "2026-08-25T11:41:03Z",
      last_error: null,
      // IBKR TWS Migration, Phase 3 readiness -- see Section 25 coverage
      // below for the provider="tws" case.
      provider: "web",
    },
    earnings_calendar: {
      state: "green",
      active_provider: "earningsapi",
      fallback_provider: "finnhub",
      last_successful_sync_at: "2026-08-25T07:22:18Z",
      events_received: 61,
      last_error: null,
      next_scheduled_sync_at: "2026-08-26T00:00:00Z",
    },
    ai_provider: {
      state: "green",
      provider: "deepseek",
      configured: true,
      last_successful_generation_at: "2026-08-24T19:55:10Z",
      last_error: null,
    },
    scheduler: {
      state: "green",
      running: true,
      registered_job_count: 4,
      last_activity_at: "2026-08-25T11:41:03Z",
      next_activity_at: "2026-08-25T12:01:17Z",
    },
    database: { state: "green", backend_healthy: true, database_healthy: true, migration_head: "69653d8b1473" },
    ...overrides,
  };
}

function marketClock(overrides: Record<string, unknown> = {}) {
  return {
    utc_now: "2026-08-25T11:45:00Z",
    new_york_now: "2026-08-25T07:45:00-04:00",
    zurich_now: "2026-08-25T13:45:00+02:00",
    market_session: "pre_market",
    next_automatic_action_job_id: "ibkr_gateway_healthcheck",
    next_automatic_action_at: "2026-08-25T12:01:17Z",
    ...overrides,
  };
}

function executionSummary(overrides: Record<string, unknown> = {}) {
  return {
    todays_events: 61,
    eligibility_passed: 61,
    eligibility_failed: 0,
    decisions_created: 0,
    waiting_for_entry: 0,
    entries_captured: 0,
    entry_failures: 0,
    settlements_due: 0,
    settled: 0,
    settlement_failures: 0,
    ...overrides,
  };
}

// Post-official-run cleanup (2026-08-27), Section 3 -- Today's Official
// Run, sourced strictly from today's real SchedulerRun/SchedulerRunEvent
// rows (schemas/api.py::TodaysOfficialRunResponse).
function todaysOfficialRun(overrides: Record<string, unknown> = {}) {
  return {
    found: true,
    run_started_at: "2026-08-25T11:41:00Z",
    run_finished_at: "2026-08-25T11:44:52Z",
    run_status: "success",
    evaluated: 62,
    skipped_ineligible: 51,
    decisions_created: 11,
    no_action: 2,
    entries_captured: 8,
    entries_failed: 1,
    pipeline_failed: 0,
    settlements_captured: 1,
    settlements_failed: 0,
    ...overrides,
  };
}

async function mockOperations(
  page: import("@playwright/test").Page,
  {
    health = healthyHealth(),
    preflight = { checks: [], ready: true, blockers: [] },
    events = [],
    jobs = [],
    failures = [],
    officialRun = todaysOfficialRun(),
    preparationProgress = {
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
    },
  }: {
    health?: Record<string, unknown>;
    preflight?: Record<string, unknown>;
    events?: Record<string, unknown>[];
    jobs?: Record<string, unknown>[];
    failures?: Record<string, unknown>[];
    officialRun?: Record<string, unknown>;
    preparationProgress?: Record<string, unknown>;
  }
) {
  await page.route("**/operations/summary", (route: Route) =>
    route.fulfill({
      json: {
        health,
        execution_summary: executionSummary(),
        official_run: officialRun,
        preflight,
        market_clock: marketClock(),
      },
    })
  );
  await page.route("**/operations/events", (route: Route) => route.fulfill({ json: { events } }));
  await page.route("**/operations/jobs", (route: Route) => route.fulfill({ json: { jobs } }));
  await page.route("**/operations/failures", (route: Route) =>
    route.fulfill({ json: { failures } })
  );
  await page.route("**/operations/preparation-progress", (route: Route) =>
    route.fulfill({ json: preparationProgress })
  );
}

const NVDA_EVENT = {
  calendar_event_id: 3139,
  symbol: "NVDA",
  company_name: "NVIDIA Corp",
  market_cap: "5045216028682.51",
  earnings_date: "2026-08-26",
  earnings_timing: "amc",
  entry_timestamp: "2026-08-26T19:55:00Z",
  exit_timestamp: "2026-08-27T19:55:00Z",
  lifecycle_state: "CALENDAR_DISCOVERED",
  lifecycle_reason: null,
  next_action: "Generate decision + capture entry",
  next_action_at: "2026-08-26T19:55:00Z",
  decision_snapshot_id: null,
  entry_capture_attempt_id: null,
  settlement_capture_attempt_id: null,
  timeline: [
    {
      label: "Earnings event synced",
      at: "2026-08-22T00:04:16Z",
      status: "done",
      detail: "Source: earningsapi",
    },
    { label: "Eligibility verified", at: null, status: "pending", detail: null },
    {
      label: "Decision generated",
      at: null,
      status: "pending",
      detail: "Scheduled: 2026-08-26T19:55:00Z",
    },
  ],
};

const XYZ_INELIGIBLE_EVENT = {
  ...NVDA_EVENT,
  calendar_event_id: 4001,
  symbol: "XYZ",
  company_name: "XYZ Test Co",
  lifecycle_state: "NOT_ELIGIBLE",
  lifecycle_reason: "market cap below $10,000,000,000",
  next_action: null,
  next_action_at: null,
};

const HEALTHCHECK_JOB = {
  job_id: "ibkr_gateway_healthcheck",
  enabled: true,
  last_run_at: "2026-08-25T11:41:03Z",
  last_run_status: "success",
  duration_ms: 105,
  items_evaluated: null,
  items_succeeded: null,
  items_failed: null,
  next_run_time: "2026-08-25T12:01:17Z",
  last_error: null,
};

const DECISION_JOB = {
  job_id: "decision_and_entry_capture",
  enabled: true,
  last_run_at: null,
  last_run_status: null,
  duration_ms: null,
  items_evaluated: null,
  items_succeeded: null,
  items_failed: null,
  next_run_time: "2026-08-25T19:55:00Z",
  last_error: null,
};

test.describe("Live Operations Monitor", () => {
  test("renders with a healthy system and READY preflight banner", async ({ page }) => {
    await mockOperations(page, {
      jobs: [HEALTHCHECK_JOB, DECISION_JOB],
      events: [NVDA_EVENT],
    });

    await page.goto("/operations");

    await expect(page.getByRole("heading", { name: "Live Operations", exact: true })).toBeVisible();
    await expect(page.getByText("READY FOR TODAY'S FORWARD TEST")).toBeVisible();
    await expect(page.getByText("GREEN").first()).toBeVisible();
  });

  // IBKR TWS Migration, Phase 3 readiness (Section 25) -- provider-aware
  // IBKR health card. live_account stays null for TWS (a real, structural
  // limitation, see services/operations.py::get_system_health's TWS
  // branch) -- this must never be shown as "Paper account" (a different,
  // false claim) nor silently dropped; the provider itself and market
  // data quality are shown instead, kept as separate concepts.
  test("System health shows the TWS provider without a false paper/live account claim", async ({
    page,
  }) => {
    await mockOperations(page, {
      health: healthyHealth({
        ibkr: {
          state: "green",
          gateway_reachable: true,
          authenticated: true,
          connected: true,
          live_account: null,
          market_data_quality: "delayed",
          last_heartbeat_at: "2026-08-25T11:41:03Z",
          last_error: null,
          provider: "tws",
        },
      }),
    });

    await page.goto("/operations");

    const systemHeading = page.getByRole("heading", { name: "System", exact: true });
    const systemCard = page.locator(".card", { has: systemHeading });
    await expect(systemCard.getByText("TWS · delayed")).toBeVisible();
    await expect(systemCard.getByText("Live account", { exact: true })).not.toBeVisible();
    await expect(systemCard.getByText("Paper account", { exact: true })).not.toBeVisible();
  });

  test("Today's Official Run shows real counts distinct from the Current Pipeline Summary", async ({
    page,
  }) => {
    // Post-official-run cleanup (2026-08-27), Section 3/4/8 -- the real
    // Aug 26 shapes: 62 evaluated, 51 ineligible, 11 decisions, 2 no
    // action, 8 captured, 1 entry failed -- distinct from the wider,
    // differently-scoped Current Pipeline Summary numbers below it.
    await mockOperations(page, {
      jobs: [HEALTHCHECK_JOB, DECISION_JOB],
      events: [NVDA_EVENT],
      officialRun: todaysOfficialRun({
        evaluated: 62,
        skipped_ineligible: 51,
        decisions_created: 11,
        no_action: 2,
        entries_captured: 8,
        entries_failed: 1,
        pipeline_failed: 0,
        settlements_captured: 1,
        settlements_failed: 0,
      }),
    });

    await page.goto("/operations");

    const officialRunCard = page.locator(".card", { hasText: "Today's Official Run" });
    await expect(officialRunCard).toBeVisible();
    const stat = (label: string) =>
      officialRunCard.locator(".stat", { has: page.getByText(label, { exact: true }) });
    await expect(stat("Evaluated").getByText("62", { exact: true })).toBeVisible();
    await expect(stat("No Action").getByText("2", { exact: true })).toBeVisible();
    await expect(stat("Entries Failed").getByText("1", { exact: true })).toBeVisible();
    await expect(stat("Entries Captured").getByText("8", { exact: true })).toBeVisible();

    // The relabeled, differently-scoped view stays present but is never
    // called "Today's" -- it's a wider pipeline window, not the official
    // run.
    await expect(page.getByRole("heading", { name: "Current Pipeline Summary" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Today's Execution Summary" })).toHaveCount(0);
  });

  test("Today's Official Run shows an honest empty state before the scheduler has fired today", async ({
    page,
  }) => {
    await mockOperations(page, {
      jobs: [HEALTHCHECK_JOB, DECISION_JOB],
      officialRun: todaysOfficialRun({ found: false }),
    });

    await page.goto("/operations");

    const officialRunCard = page.locator(".card", { hasText: "Today's Official Run" });
    await expect(officialRunCard).toBeVisible();
    await expect(officialRunCard.getByText(/hasn't fired/)).toBeVisible();
  });

  test("shows a nav link to Operations under an Operations section", async ({ page }) => {
    await mockOperations(page, {});

    await page.goto("/");

    // Renamed "Live Operations Monitor" -> "Live Operations" by the V4
    // product consolidation (2026-09-02). What this test protects is
    // unchanged: the Operations surface has its own nav section and a
    // working link, so it can never be folded into another heading or
    // silently dropped.
    await expect(page.getByText("Operations", { exact: true })).toBeVisible();
    await expect(page.getByRole("link", { name: "Live Operations" })).toBeVisible();
  });

  test("V4 leads the navigation and V3 is retained as control", async ({ page }) => {
    await mockOperations(page, {});

    await page.goto("/");

    // V4 product consolidation: the decision engine and its forward test
    // are the primary surfaces; V3 remains reachable, relabelled as the
    // historical control cohort rather than deleted.
    await expect(page.getByText("Decision Engine", { exact: true })).toBeVisible();
    await expect(page.getByRole("link", { name: "V4 Decision Lab" })).toBeVisible();
    await expect(
      page.getByRole("link", { name: "V4 Forward Track Record" }),
    ).toBeVisible();
    await expect(
      page.getByRole("link", { name: "V3 Control Track Record" }),
    ).toBeVisible();

    // Legacy surfaces are retained under their own heading -- not removed.
    await expect(page.getByText("Legacy / Control", { exact: true })).toBeVisible();
    await expect(page.getByRole("link", { name: "Cross-Company Replay" })).toBeVisible();
  });

  test("renders a critical banner and NOT READY when the scheduler is down", async ({ page }) => {
    await mockOperations(page, {
      health: healthyHealth({
        scheduler: {
          state: "red",
          running: false,
          registered_job_count: 4,
          last_activity_at: null,
          next_activity_at: null,
        },
      }),
      preflight: {
        checks: [{ label: "Scheduler running", passed: false, detail: null }],
        ready: false,
        blockers: ["Scheduler running"],
      },
    });

    await page.goto("/operations");

    await expect(page.getByText("CRITICAL: Scheduler not running.")).toBeVisible();
    await expect(page.getByText(/NOT READY/)).toBeVisible();
    await expect(page.getByText("RED").first()).toBeVisible();
  });

  test("surfaces a backend-detected missed-job alert prominently", async ({ page }) => {
    await mockOperations(page, {
      jobs: [HEALTHCHECK_JOB, DECISION_JOB],
      failures: [
        {
          occurred_at: "2026-08-25T19:50:00Z",
          symbol: null,
          stage: "decision_and_entry_capture",
          category: "missed_job",
          explanation:
            "Scheduler job decision_and_entry_capture was due at 2026-08-25T19:55:00Z but has not started -- 12 minutes overdue",
          detail: null,
          retryability: "RETRYABLE",
        },
      ],
    });

    await page.goto("/operations");

    await expect(page.getByText(/CRITICAL:.*was due at.*has not started/)).toBeVisible();
    await expect(page.getByText(/was due at.*has not started/).last()).toBeVisible();
  });

  test("aggregates many same-category alerts into one summary banner, not one per event", async ({
    page,
  }) => {
    // Real live-observed scenario: a full day's worth of due-but-
    // unprocessed events (24 of them, before the forward test had
    // actually started running) each produced their own, individually
    // real FailureEntry -- rendering 24 separate top-of-page CRITICAL
    // banners would drown the page. This locks in the fix: one summary
    // line, not one banner per symbol.
    const symbols = ["DKS", "BZ", "VIPS", "MZTI", "CSHR", "CTRN", "GTEN", "SHMD"];
    await mockOperations(page, {
      jobs: [HEALTHCHECK_JOB, DECISION_JOB],
      failures: symbols.map((symbol) => ({
        occurred_at: "2026-08-24T19:55:00Z",
        symbol,
        stage: "decision",
        category: "unprocessed_due_event",
        explanation: `${symbol} was due for Generate decision + capture entry at 2026-08-24T19:55:00Z but shows no decision/entry activity yet`,
        detail: null,
        retryability: "RETRYABLE",
      })),
    });

    await page.goto("/operations");

    // The Failure Center table below is expected to still list all 8
    // individual entries in full detail -- only the top-of-page banner
    // strip must be a single summarized line, so this scopes its check
    // to just the .notice-critical banner elements, not the whole page.
    const banners = page.locator(".notice-critical");
    await expect(banners.filter({ hasText: `${symbols.length} due event(s)` })).toHaveCount(1);
    await expect(
      banners.filter({ hasText: "but shows no decision/entry activity yet" })
    ).toHaveCount(0);
  });

  test("shows live preparation progress while a worker is actively claimed", async ({ page }) => {
    await mockOperations(page, {
      preparationProgress: {
        queue_depth: 3,
        completed: 2,
        failed: 0,
        worker_active: true,
        current_symbol: "SNPS",
        current_stage: "SEC filings",
        step_index: 5,
        step_total: 8,
        attempt: 1,
        heartbeat_seconds_ago: 8,
        elapsed_seconds: 64,
      },
    });

    await page.goto("/operations");

    const heading = page.getByRole("heading", { name: "Research Preparation" });
    await expect(heading).toBeVisible();
    const card = page.locator(".card", { has: heading });
    await expect(card.getByText("3 pending")).toBeVisible();
    await expect(card.getByText("SNPS")).toBeVisible();
    await expect(card.getByText("SEC filings (5 / 8)")).toBeVisible();
    await expect(card.getByText("8s ago")).toBeVisible();
    await expect(card.getByText("01:04")).toBeVisible();
  });

  test("shows pending queue depth even while no worker is currently claimed", async ({
    page,
  }) => {
    await mockOperations(page, {
      preparationProgress: {
        queue_depth: 2,
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
      },
    });

    await page.goto("/operations");

    const heading = page.getByRole("heading", { name: "Research Preparation" });
    await expect(heading).toBeVisible();
    const card = page.locator(".card", { has: heading });
    await expect(card.getByText("2 pending")).toBeVisible();
    await expect(card.getByText("idle")).toBeVisible();
  });

  test("hides preparation progress when the queue is entirely empty", async ({ page }) => {
    await mockOperations(page, {});

    await page.goto("/operations");

    await expect(page.getByRole("heading", { name: "Research Preparation" })).toHaveCount(0);
  });

  test("scheduler job rows show real job status and next run", async ({ page }) => {
    await mockOperations(page, { jobs: [HEALTHCHECK_JOB, DECISION_JOB] });

    await page.goto("/operations");

    const jobsHeading = page.getByRole("heading", { name: "Scheduler Jobs" });
    await expect(jobsHeading).toBeVisible();
    const jobsCard = page.locator(".card", { has: jobsHeading });
    // IBKR TWS Migration, Phase 3 readiness (Section 29) -- display label
    // only, provider-neutral now that this job runs against either
    // transport; the persisted job_id itself ("ibkr_gateway_healthcheck")
    // is unchanged, see HEALTHCHECK_JOB below.
    await expect(jobsCard.getByText("IBKR Provider Healthcheck")).toBeVisible();
    await expect(jobsCard.getByText("READY")).toBeVisible();
    await expect(jobsCard.getByText("Decision + Entry Capture")).toBeVisible();
    await expect(jobsCard.getByText("NO RUNS YET")).toBeVisible();
  });

  test("pipeline rows show real lifecycle states, including an ineligible one with its reason", async ({
    page,
  }) => {
    await mockOperations(page, { events: [NVDA_EVENT, XYZ_INELIGIBLE_EVENT] });

    await page.goto("/operations");

    const pipelineHeading = page.getByRole("heading", { name: "Today's Earnings Pipeline" });
    await expect(pipelineHeading).toBeVisible();
    const pipelineCard = page.locator(".card", { has: pipelineHeading });
    await expect(pipelineCard.locator("tbody").getByText("CALENDAR DISCOVERED")).toBeVisible();
    await expect(pipelineCard.locator("tbody").getByText("NOT ELIGIBLE")).toBeVisible();
    await expect(pipelineCard.locator("tbody").getByText("market cap below $10,000,000,000")).toBeVisible();
  });

  test("pipeline rows show real automatic research preparation states", async ({ page }) => {
    // Pre-live hardening Section 8 -- FILTERED_OUT/READY_FOR_DECISION/
    // PREPARATION_FAILED all come from a real preparation-stage
    // SchedulerRunEvent (services/earnings_research_preparation.py),
    // never a manual Search action.
    const readyEvent = {
      ...NVDA_EVENT,
      calendar_event_id: 5001,
      symbol: "READY",
      company_name: "Ready Research Co",
      lifecycle_state: "READY_FOR_DECISION",
      lifecycle_reason: null,
    };
    const filteredEvent = {
      ...NVDA_EVENT,
      calendar_event_id: 5002,
      symbol: "SMALLCAP",
      company_name: "Too Small Co",
      lifecycle_state: "FILTERED_OUT",
      lifecycle_reason: "market cap below $10,000,000,000",
      next_action: null,
      next_action_at: null,
    };
    const failedEvent = {
      ...NVDA_EVENT,
      calendar_event_id: 5003,
      symbol: "PREPFAIL",
      company_name: "Prep Failed Co",
      lifecycle_state: "PREPARATION_FAILED",
      lifecycle_reason: "SEC EDGAR outage",
      next_action: "Retry research preparation",
      next_action_at: null,
    };
    await mockOperations(page, { events: [readyEvent, filteredEvent, failedEvent] });

    await page.goto("/operations");

    const pipelineHeading = page.getByRole("heading", { name: "Today's Earnings Pipeline" });
    const pipelineCard = page.locator(".card", { has: pipelineHeading });
    await expect(pipelineCard.locator("tbody").getByText("READY FOR DECISION")).toBeVisible();
    await expect(pipelineCard.locator("tbody").getByText("FILTERED OUT")).toBeVisible();
    await expect(pipelineCard.locator("tbody").getByText("PREPARATION FAILED")).toBeVisible();
    await expect(pipelineCard.locator("tbody").getByText("SEC EDGAR outage")).toBeVisible();
  });

  test("a no-action decision shows NO ACTION, never ENTRY FAILED", async ({ page }) => {
    // Post-live correction (2026-08-25) -- real Aug 25 SJM shape: the
    // strategy engine genuinely recommended nothing, but
    // capture_benchmark_entry still records a real FAILED
    // EntryCaptureAttempt for it ("no recommended strategy legs to
    // enter"). This must render as a real, non-error outcome, never the
    // same red state as an actual infrastructure entry-capture failure.
    const noActionEvent = {
      ...NVDA_EVENT,
      calendar_event_id: 6001,
      symbol: "SJM",
      company_name: "J.M. Smucker Co",
      lifecycle_state: "NO_ACTION",
      lifecycle_reason: "the strategy engine found no actionable strategy for this event",
      next_action: null,
      next_action_at: null,
    };
    await mockOperations(page, { events: [noActionEvent] });

    await page.goto("/operations");

    const pipelineCard = page.locator(".card", {
      has: page.getByRole("heading", { name: "Today's Earnings Pipeline" }),
    });
    await expect(pipelineCard.locator("tbody").getByText("NO ACTION", { exact: true })).toBeVisible();
    await expect(pipelineCard.locator("tbody").getByText("ENTRY FAILED")).toHaveCount(0);
  });

  test("retry entry capture is hidden once the legal capture window has closed", async ({
    page,
  }) => {
    // Post-live correction (2026-08-25) Section 15 -- real Aug 25
    // evidence: INTU/HEI/ZM/SMTC's entries failed inside the legal
    // window, and Operations kept advertising "Retry entry capture"
    // hours later even though the backend (capture_benchmark_entry's
    // own _verify_no_lookahead) would already refuse it. The backend
    // now omits next_action once the window has closed; this locks in
    // that the frontend just reflects it honestly, never inventing its
    // own action.
    const stillWithinWindow = {
      ...NVDA_EVENT,
      calendar_event_id: 6002,
      symbol: "FRESH",
      company_name: "Freshly Failed Co",
      lifecycle_state: "ENTRY_FAILED",
      lifecycle_reason: "no ask quote available for a long leg",
      next_action: "Retry entry capture",
      next_action_at: null,
    };
    const windowClosed = {
      ...NVDA_EVENT,
      calendar_event_id: 6003,
      symbol: "STALE",
      company_name: "Stale Failed Co",
      lifecycle_state: "ENTRY_FAILED",
      lifecycle_reason: "no ask quote available for a long leg",
      next_action: null,
      next_action_at: null,
    };
    await mockOperations(page, { events: [stillWithinWindow, windowClosed] });

    await page.goto("/operations");

    const pipelineCard = page.locator(".card", {
      has: page.getByRole("heading", { name: "Today's Earnings Pipeline" }),
    });
    const rows = pipelineCard.locator("tbody tr");
    await expect(rows.filter({ hasText: "FRESH" }).getByText("Retry entry capture")).toBeVisible();
    await expect(rows.filter({ hasText: "STALE" }).getByText("Retry entry capture")).toHaveCount(
      0
    );
  });

  test("a transient preparation-time failure shows as an amber warning, not a red fatal failure", async ({
    page,
  }) => {
    // Post-live correction (2026-08-25) Section 8 -- real Aug 25 WSM
    // evidence: a rate-limited preparation-time options-chain probe
    // used to render identically to a real fatal preparation failure,
    // even though WSM's own later, independent execution-time
    // eligibility check succeeded and produced a real DecisionSnapshot
    // moments afterward.
    const warnedThenReady = {
      ...NVDA_EVENT,
      calendar_event_id: 6004,
      symbol: "WSM",
      company_name: "Williams-Sonoma Inc",
      lifecycle_state: "WAITING_FOR_DECISION",
      lifecycle_reason: null,
      next_action: "Generate decision + capture entry",
      timeline: [
        NVDA_EVENT.timeline[0],
        {
          label: "Research prepared",
          at: "2026-08-25T17:13:47Z",
          status: "warning",
          detail: "options chain lookup failed: IBKR Client Portal Gateway rate-limited the request",
        },
        {
          label: "Eligibility verified",
          at: "2026-08-25T19:55:20Z",
          status: "done",
          detail: null,
        },
      ],
    };
    await mockOperations(page, { events: [warnedThenReady] });

    await page.goto("/operations");

    const pipelineCard = page.locator(".card", {
      has: page.getByRole("heading", { name: "Today's Earnings Pipeline" }),
    });
    await page.getByRole("row", { name: /WSM/ }).click();
    await expect(pipelineCard.locator("tbody").getByText("rate-limited the request")).toBeVisible();
    // The overall row must never read FILTERED_OUT/PREPARATION_FAILED --
    // the warning is real but non-blocking.
    await expect(pipelineCard.locator("tbody").getByText("FILTERED OUT")).toHaveCount(0);
    await expect(pipelineCard.locator("tbody").getByText("PREPARATION FAILED")).toHaveCount(0);
  });

  test("has no force-decision/force-entry/force-settlement/override controls anywhere", async ({
    page,
  }) => {
    await mockOperations(page, {
      jobs: [HEALTHCHECK_JOB, DECISION_JOB],
      events: [NVDA_EVENT, XYZ_INELIGIBLE_EVENT],
      failures: [
        {
          occurred_at: "2026-08-25T06:21:30Z",
          symbol: null,
          stage: "options",
          category: "ibkr auth_failed",
          explanation: "ibkr (options) reported auth_failed",
          detail: "log in at the Gateway's own web page",
          retryability: "RETRYABLE",
        },
      ],
    });

    await page.goto("/operations");
    await expect(page.getByRole("heading", { name: "Live Operations", exact: true })).toBeVisible();

    for (const forbidden of [
      "Force Decision",
      "Force Entry",
      "Force Settlement",
      "Override Eligibility",
      "Change Quote",
      "Backfill",
      "Change Strategy",
      "Retry",
      "Run Now",
    ]) {
      await expect(page.getByRole("button", { name: forbidden })).toHaveCount(0);
    }
  });

  test("clicking a ticker navigates to the existing company workspace", async ({ page }) => {
    await mockOperations(page, { events: [NVDA_EVENT] });

    await page.goto("/operations");
    await page.getByRole("link", { name: "NVDA" }).click();

    await expect(page).toHaveURL(/\/company\/NVDA$/);
  });
});
