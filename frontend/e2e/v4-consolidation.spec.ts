import { test, expect, type Page, type Route } from "@playwright/test";

/**
 * V4 consolidation -- deterministic, fully mocked coverage of the V4-first
 * product surfaces (Section 55). Every backend call is intercepted with
 * page.route(); this suite never reaches a real backend, TWS, or database,
 * and can never create a real decision.
 */

const NOW = "2026-09-10T19:30:00+00:00";

const healthy = {
  ibkr: { state: "green", gateway_reachable: true, authenticated: true, connected: true, live_account: null, market_data_quality: "delayed", last_heartbeat_at: NOW, last_error: null, provider: "tws" },
  earnings_calendar: { state: "green", active_provider: "earningsapi", fallback_provider: "finnhub", last_successful_sync_at: NOW, events_received: 85, last_error: null, next_scheduled_sync_at: NOW },
  ai_provider: { state: "green", provider: "deepseek", configured: true, last_successful_generation_at: NOW, last_error: null },
  scheduler: { state: "green", running: true, registered_job_count: 5, last_activity_at: null, next_activity_at: NOW },
  database: { state: "green", backend_healthy: true, database_healthy: true, migration_head: "f4b6d8e0c2a3" },
  v4_shadow: { state: "green", enabled: false, decisions_today: 0, ranked_today: 0, no_action_today: 0, failed_today: 0, entry_observations_failed_today: 0, settlements_due: 0, settlements_complete: 0, last_run_at: null, engine_version: "options-decision-engine-v4", decision_time_et: "15:30", settlement_time_et: "15:30", timing_policy_version: "v4-1530-entry-1530-t1-settlement-v2", note: "V4 forward test" },
};

const decisionSummary = (over: Record<string, unknown> = {}) => ({
  id: 1, earnings_calendar_event_id: 2913, ticker: "PANW", company_name: "Palo Alto Networks Inc",
  legal_decision_window_at: NOW, generated_at: NOW, as_of: NOW, status: "RANKED",
  no_action_reason: null, failure_category: null, rank_1_candidate_id: "spread",
  candidate_count: 3, rankable_candidate_count: 3,
  view: { direction: "bearish", volatility: "long_vol", expected_move_intent: "large_move", confidence: "medium", reasoning: "synthetic" },
  provenance: { llm_provider: "deepseek", llm_model: "deepseek-v4-flash", prompt_version: "decision-view-v1", decision_view_schema_version: "v1" },
  market_data: { underlying_price: "339.90", underlying_quote_at: NOW, market_data_quality: "delayed", source_provider: "ibkr_tws", max_input_skew_seconds: "0" },
  versions: { engine: "options-decision-engine-v4", ranking: "v4-4b-t1-executable-ranking-v1" },
  timing_policy_version: "v4-pre-earnings-1530et-v1",
  expected_move: { spot: "339.90", observed_at: NOW, implied_move_available: true, implied_move_dollars: "17.00", implied_move_pct: "0.05", upper_implied_boundary: "356.90", lower_implied_boundary: "322.90", implied_move_source: "atm_straddle", historical_sample_n: 8, historical_evidence_quality: "adequate", historical_median_abs_move_pct: "0.04", historical_median_upper_boundary: "353.50", historical_median_lower_boundary: "326.30", context_version: "test" },
  notice: "V4 FORWARD TEST -- PROSPECTIVE EVIDENCE ONLY -- NO BROKERAGE ORDER.",
  ...over,
});

const cand = (id: string, strategy: string, worst: string, median: string, cash: string) => ({
  candidate_id: id, unconstrained_rank: 1, strategy, expiration: "2026-09-18", validity_status: "RANKABLE",
  semantic_tier: "compatible", core_worst_return: worst, core_median_return: median,
  core_positive_scenario_fraction: "0.57", stress_worst_return: "-0.40", mean_relative_spread: "0.05",
  entry_cash_required: cash, market_data_quality: "delayed", rank_explanation: "better downside band than #2",
});

const cell = (em: string, iv: string, ret: string) => ({
  scenario_id: `${em}_${iv}`, move_label: `${Number(em) >= 0 ? "+" : ""}${em} EM`, em_fraction: em,
  scenario_underlying_price: "339.90", iv_label: iv, iv_multiplier: iv === "crush" ? "0.7" : iv === "flat" ? "1.0" : "1.2",
  return_executable: ret, return_theoretical: ret, reason_codes: [],
});

const fullCandidate = (id: string, strategy: string, legs: { action: string; right: string; strike: string; bid: string; ask: string }[]) => ({
  candidate_id: id, rank: 1, strategy, expiration: "2026-09-18", geometry_variant_id: null, validity_status: "RANKABLE", status_reason: null,
  semantic: { compatibility: "0.9", tier: "compatible" },
  core: { worst_return: "-0.30", median_return: "0.05", best_return: "0.40", positive_scenario_fraction: "0.57", positive_region_count: 4, region_count: 7, scenarios_valued: 21, no_profitable_region: false, profit_concentrated_in_single_region: false },
  tail_stress: { worst_return: "-0.40", large_move_survival: "0.5", vs_core_worst_delta: "-0.10", scenarios_valued: 12, note: "stress" },
  execution: { mean_relative_spread: "0.05", worst_relative_spread: "0.07", two_sided_leg_count: legs.length, leg_count: legs.length, required_sides_complete: true, max_leg_timestamp_skew_seconds: "0", market_data_quality: "delayed" },
  capital: { standardized_capital: "2000", entry_cash_required: "180", capital_utilisation: "0.09" },
  rank_explanation: "better downside band than #2",
  scenario_grid: {
    core: ["-1", "-0.5", "0", "0.5", "1"].flatMap((em) => ["crush", "flat", "expand"].map((iv) => cell(em, iv, em === "0" ? "-0.10" : "0.12"))),
    stress: ["-2", "-1.5", "1.5", "2"].flatMap((em) => ["crush", "flat", "expand"].map((iv) => cell(em, iv, "-0.35"))),
  },
  legs: legs.map((l, i) => ({ leg_index: i, action: l.action, right: l.right, strike: l.strike, quantity: 1, external_contract_id: `9128495${i}`, required_side: l.action === "buy" ? "ask" : "bid", required_side_price: l.action === "buy" ? l.ask : l.bid, bid: l.bid, ask: l.ask, implied_volatility: "1.30", market_data_quality: "delayed", source_provider: "ibkr_tws", retrieved_at: NOW })),
});

const configResult = (key: string, capital: string, risk: string, maxRisk: string, status: string, rank1: string | null, exclusions: unknown[] = []) => ({
  configuration_key: key, label: `$${Number(capital).toLocaleString()} ${risk[0].toUpperCase()}${risk.slice(1)}`,
  capital_base: capital, risk_profile: risk, configuration_version: "v4-forward-configurations-v1",
  max_risk_dollars: maxRisk, max_risk_utilization_pct: risk === "conservative" ? "15" : risk === "moderate" ? "30" : "50",
  status, no_action_reason: status === "NO_ACTION" ? `${key}: no candidate was eligible. Risk cap exceeded: 1 contract risks $1,155.00, but Moderate allows $600.00 max risk (30% of $2,000 standardized capital)` : null,
  rank_1_candidate_id: rank1, eligible_candidate_count: rank1 ? 1 : 0, excluded_candidate_count: exclusions.length,
  exclusions, ranked_candidate_ids: rank1 ? [rank1, "long_put"] : [], ranking_version: "v4-4b-t1-executable-ranking-v1",
  rank_1: rank1 ? cand(rank1, rank1 === "spread" ? "bull_call_spread" : "long_put", "-0.30", "0.05", rank1 === "spread" ? "180" : "1155") : null,
});

const exclusion = { candidate_id: "long_put", reason_code: "RISK_CAP_EXCEEDED", detail: "Risk cap exceeded: 1 contract risks $1,155.00, but Moderate allows $600.00 max risk (30% of $2,000 standardized capital)" };

function configurationsResponse(noActionAtTwoK = false) {
  return {
    notice: "V4 FORWARD TEST -- PROSPECTIVE EVIDENCE ONLY -- NO BROKERAGE ORDER.",
    decision: decisionSummary(),
    timing_policy_version: "v4-pre-earnings-1530et-v1",
    configurations: [
      configResult("v4_2k_conservative", "2000", "conservative", "300", "NO_ACTION", null, [exclusion]),
      configResult("v4_2k_moderate", "2000", "moderate", "600", noActionAtTwoK ? "NO_ACTION" : "RANKED", noActionAtTwoK ? null : "spread", [exclusion]),
      configResult("v4_2k_aggressive", "2000", "aggressive", "1000", "RANKED", "spread", [exclusion]),
      configResult("v4_10k_conservative", "10000", "conservative", "1500", "RANKED", "spread"),
      configResult("v4_10k_moderate", "10000", "moderate", "3000", "RANKED", "long_put"),
      configResult("v4_10k_aggressive", "10000", "aggressive", "5000", "RANKED", "long_put"),
    ],
    candidates: [cand("spread", "bull_call_spread", "-0.30", "0.05", "180"), cand("long_put", "long_put", "-0.45", "0.08", "1155")],
    default_configuration_key: "v4_2k_moderate",
  };
}

const json = (body: unknown) => (route: Route) => route.fulfill({ json: body });

async function mockCommon(page: Page, { v4Decisions = [decisionSummary()] as unknown[], mockOps = true } = {}) {
  if (mockOps) {
    await page.route("**/operations/summary", json({
      health: healthy,
      today: { decision_window_et: "15:30", settlement_window_et: "15:30", deadline_et: "15:50", events_in_window: 1, business_eligible: 1, research_ready: 1, waiting_decision: 1, decisions_today: 0, ranked_today: 0, no_action_today: 0, entries_observed_today: 0, entries_failed_today: 0, deadline_skipped_today: 0, research_not_ready_today: 0, settlements_due_today: 0, settled_today: 0, settlements_failed_today: 0 },
      readiness: { window_days: 7, upcoming_events: 1, business_eligible: 1, company_resolved: 1, research_queued: 0, research_running: 0, research_ready: 1, research_failed: 0, ai_thesis_ready: 1, v4_decision_ready: 1, next_window_at: "2026-09-03T19:30:00Z", next_window_ready: 1, next_window_total: 1 },
      staleness: [],
      preflight: { checks: [], ready: true, blockers: [] },
      market_clock: { utc_now: NOW, new_york_now: "2026-09-10T15:30:00-04:00", zurich_now: "2026-09-10T21:30:00+02:00", market_session: "open", next_automatic_action_job_id: "ibkr_gateway_healthcheck", next_automatic_action_at: NOW, settlement_window_tolerance_minutes: 5 },
    }));
  }
  await page.route("**/operations/preparation-progress", json({ queue_depth: 0, completed: 3, failed: 0, worker_active: false, current_symbol: null, current_stage: null, step_index: null, step_total: null, attempt: null, heartbeat_seconds_ago: null, elapsed_seconds: null }));
  await page.route("**/operations/events", json({ events: [] }));
  await page.route("**/operations/jobs", json({ jobs: [] }));
  await page.route("**/operations/failures", json({ failures: [] }));
  await page.route("**/v4/shadow/decisions", json({ notice: "EXPERIMENTAL", decisions: v4Decisions }));
  await page.route("**/v4/shadow/track-record", json({ notice: "EXPERIMENTAL", cohort: "v4", counts: { shadow_decisions: v4Decisions.length, ranked: v4Decisions.length, no_action: 0, failed: 0, entry_observed: 0, entry_not_executable: 0, settled: 0, settlement_failed: 0 }, sample_sufficiency: "INSUFFICIENT SAMPLE" }));
  await page.route("**/v4/shadow/track-record/by-configuration", json({
    notice: "EXPERIMENTAL", sample_floor: 30,
    metrics_note: "Counts only. No portfolio drawdown or Sharpe is computed: there is no real capital ledger yet.",
    configurations: ["v4_2k_conservative", "v4_2k_moderate", "v4_2k_aggressive", "v4_10k_conservative", "v4_10k_moderate", "v4_10k_aggressive"].map((k) => ({
      configuration_key: k, events: v4Decisions.length, actionable: k.includes("conservative") && k.includes("2k") ? 0 : v4Decisions.length, no_action: k.includes("conservative") && k.includes("2k") ? v4Decisions.length : 0, failed: 0,
      entry_observed: null, entry_failed: null, settled: null, settlement_failed: null, sample_sufficiency: "INSUFFICIENT SAMPLE",
    })),
  }));
  await page.route("**/v4/shadow/decisions/1/configurations", json(configurationsResponse()));
  await page.route("**/v4/shadow/decisions/1/candidates", json({ notice: "EXPERIMENTAL", candidates: [
    fullCandidate("spread", "bull_call_spread", [{ action: "buy", right: "call", strike: "340", bid: "3.00", ask: "3.20" }, { action: "sell", right: "call", strike: "350", bid: "1.20", ask: "1.40" }]),
    fullCandidate("long_put", "long_put", [{ action: "buy", right: "put", strike: "347.5", bid: "10.90", ask: "11.55" }]),
  ] }));
}

test.describe("Dashboard (V4-first)", () => {
  test("shows today, V4 decisions, readiness and small-sample warning from real endpoints", async ({ page }) => {
    await mockCommon(page);
    await page.goto("/");
    await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();
    await expect(page.getByTestId("dashboard-today")).toContainText("TWS · DELAYED");
    await expect(page.getByTestId("dashboard-today")).toContainText("Settlement 15:30 ET, T+1");
    await expect(page.getByTestId("dashboard-readiness")).toContainText("Disabled");
    await expect(page.getByTestId("dashboard-v4-decisions")).toContainText("PANW");
    await expect(page.getByTestId("dashboard-performance")).toContainText("INSUFFICIENT SAMPLE");
  });

  test("empty V4 state is honest", async ({ page }) => {
    await mockCommon(page, { v4Decisions: [] });
    await page.goto("/");
    await expect(page.getByTestId("dashboard-v4-decisions")).toContainText("No V4 decisions yet");
  });
});

test.describe("V4 Decision Lab", () => {
  test("hero, selector, comparison, why-this-strategy, expected move and both matrices render from frozen evidence", async ({ page }) => {
    await mockCommon(page);
    await page.goto("/v4-decision-lab/1");
    await expect(page.getByText("PANW")).toBeVisible();
    await expect(page.getByText("BEARISH")).toBeVisible();
    await expect(page.getByText("TWS · DELAYED")).toBeVisible();
    await expect(page.getByTestId("config-selector")).toBeVisible();
    await expect(page.getByTestId("config-comparison")).toContainText("$10,000 Aggressive");
    await expect(page.getByTestId("why-this-strategy")).toContainText("Why this strategy ranked first");
    await expect(page.getByTestId("why-this-strategy")).toContainText("Why not the alternatives");
    await expect(page.getByTestId("expected-move-chart")).toBeVisible();
    await expect(page.getByTestId("scenario-matrix-core")).toContainText("IV");
    await expect(page.getByTestId("scenario-matrix-stress")).toContainText("tail stress");
    await expect(page.getByTestId("candidate-explorer")).toContainText("Bull Call Spread");
  });

  test("switching configuration is client-side only and shows NO_ACTION honestly", async ({ page }) => {
    await mockCommon(page);
    let configurationCalls = 0;
    await page.route("**/v4/shadow/decisions/1/configurations", (route) => { configurationCalls += 1; return route.fulfill({ json: configurationsResponse() }); });
    await page.goto("/v4-decision-lab/1");
    await expect(page.getByTestId("config-comparison")).toBeVisible();
    const before = configurationCalls;
    await page.getByRole("button", { name: "Conservative" }).click();
    await expect(page.getByText("No action for $2,000 Conservative")).toBeVisible();
    await page.getByRole("button", { name: "$10,000" }).click();
    await page.getByRole("button", { name: "Moderate" }).click();
    await expect(page.getByText("Long Put").first()).toBeVisible();
    expect(configurationCalls).toBe(before);
  });

  test("candidate explorer expands legs with conId, sides and quality", async ({ page }) => {
    await mockCommon(page);
    await page.goto("/candidate-explorer/1");
    await expect(page.getByRole("heading", { name: "Candidate Explorer", level: 1 })).toBeVisible();
    await page.getByText("Bull Call Spread").first().click();
    await expect(page.getByText("91284950")).toBeVisible();
    await expect(page.getByText("ASK").first()).toBeVisible();
  });

  test("empty state when no V4 decisions exist", async ({ page }) => {
    await mockCommon(page, { v4Decisions: [] });
    await page.goto("/v4-decision-lab");
    await expect(page.getByText("No V4 decisions yet.")).toBeVisible();
  });
});

test.describe("Performance", () => {
  test("V4 forward track record shows six cohorts and INSUFFICIENT SAMPLE, never portfolio stats", async ({ page }) => {
    await mockCommon(page);
    await page.goto("/v4-shadow-track-record");
    await expect(page.getByTestId("cohort-selector")).toContainText("$10,000 Aggressive");
    await expect(page.getByTestId("insufficient-sample")).toBeVisible();
    await page.getByRole("button", { name: "$2,000 Conservative" }).click();
    await expect(page.getByText("$2,000 Conservative").nth(1)).toBeVisible();
    await expect(page.locator("body")).not.toContainText(/Sharpe ratio/i);
  });

});


test.describe("Operations", () => {
  test("is V4-only and shows the disabled forward engine honestly", async ({ page }) => {
    await mockCommon(page);
    await page.goto("/operations");
    await expect(page.getByRole("heading", { name: "Live Operations" })).toBeVisible();
    await expect(page.getByText("Control / Official — V3")).toHaveCount(0);
    await expect(page.getByText("Legacy / Control")).toHaveCount(0);
    await expect(page.getByTestId("operations-v4")).toContainText("15:30 ET");
    await expect(page.getByTestId("operations-v4")).toContainText("DISABLED");
    await expect(page.getByTestId("ops-today")).toContainText("15:30 ET");
    await expect(page.getByTestId("ops-readiness")).toContainText("V4 decision ready");
  });
});


test.describe("AI Research", () => {
  test("shows a live preparing state instead of a generic failure", async ({ page }) => {
    await mockCommon(page, { mockOps: false });
    await page.route("**/research/query", json({
      question: "What did PANW guide?", status: "preparing", answer: null, citations: [], trace: null,
      preparing: [{ ticker: "PANW", company_name: "Palo Alto Networks Inc" }], unresolved_tickers: [],
    }));
    await page.goto("/research");
    await page.getByPlaceholder(/Ask about a covered company/).fill("What did PANW guide?");
    await page.locator("button.btn").first().click();
    await expect(page.getByTestId("research-preparing")).toContainText("Preparing research for PANW");
  });
});
