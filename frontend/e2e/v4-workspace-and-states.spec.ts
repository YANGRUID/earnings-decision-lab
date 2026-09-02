import { test, expect, type Page, type Route } from "@playwright/test";

/**
 * V4 company workspace and forward-outcome states (Sections 23-27, 59).
 * Fully mocked V4 endpoints; the deterministic fixture backend (:8011,
 * fixture options provider, ZZE2E1 seeded by globalSetup) serves the
 * company-level V3 data. Never touches production.
 */
const NOW = "2026-09-10T19:30:00+00:00";
const TICKER = "ZZE2E1";
const json = (body: unknown) => (route: Route) => route.fulfill({ json: body });

const decision = (over: Record<string, unknown> = {}) => ({
  id: 7, earnings_calendar_event_id: 4242, ticker: TICKER, company_name: "E2E Fixture Co",
  legal_decision_window_at: NOW, generated_at: NOW, as_of: NOW, status: "RANKED",
  no_action_reason: null, failure_category: null, rank_1_candidate_id: "spread",
  candidate_count: 2, rankable_candidate_count: 2,
  view: { direction: "bullish", volatility: "long_vol", expected_move_intent: "large_move", confidence: "medium", reasoning: "Synthetic reasoning for E2E." },
  provenance: { llm_provider: "deepseek", llm_model: "deepseek-v4-flash", prompt_version: "decision-view-v1", decision_view_schema_version: "v1" },
  market_data: { underlying_price: "100.00", underlying_quote_at: NOW, market_data_quality: "delayed", source_provider: "ibkr_tws", max_input_skew_seconds: "0" },
  versions: { engine: "options-decision-engine-v4", ranking: "v4-4b-t1-executable-ranking-v1" },
  timing_policy_version: "v4-pre-earnings-1530et-v1",
  expected_move: { spot: "100.00", observed_at: NOW, implied_move_available: true, implied_move_dollars: "5.00", implied_move_pct: "0.05", upper_implied_boundary: "105", lower_implied_boundary: "95", implied_move_source: "atm_straddle", historical_sample_n: 8, historical_evidence_quality: "adequate", historical_median_abs_move_pct: "0.04", historical_median_upper_boundary: "104", historical_median_lower_boundary: "96", context_version: "test" },
  notice: "EXPERIMENTAL",
  ...over,
});
const cand = (id: string, strategy: string, cash: string) => ({
  candidate_id: id, unconstrained_rank: 1, strategy, expiration: "2026-09-18", validity_status: "RANKABLE", semantic_tier: "compatible",
  core_worst_return: "-0.30", core_median_return: "0.05", core_positive_scenario_fraction: "0.57", stress_worst_return: "-0.40",
  mean_relative_spread: "0.05", entry_cash_required: cash, market_data_quality: "delayed", rank_explanation: "better downside band",
});
const cfg = (key: string, cap: string, risk: string, maxRisk: string, status: string, rank1: string | null, lifecycle: string) => ({
  configuration_key: key, label: `$${Number(cap).toLocaleString()} ${risk[0].toUpperCase()}${risk.slice(1)}`, capital_base: cap, risk_profile: risk,
  configuration_version: "v4-forward-configurations-v1", max_risk_dollars: maxRisk, max_risk_utilization_pct: "30",
  status, no_action_reason: status === "NO_ACTION" ? "No candidate satisfied the methodology for this configuration." : null,
  rank_1_candidate_id: rank1, eligible_candidate_count: rank1 ? 1 : 0, excluded_candidate_count: rank1 ? 0 : 2, exclusions: [],
  ranked_candidate_ids: rank1 ? [rank1] : [], ranking_version: "v4-4b-t1-executable-ranking-v1",
  rank_1: rank1 ? cand(rank1, "bull_call_spread", "180") : null, lifecycle,
});
function configurations(state: "waiting" | "entry_failed" | "settlement_pending" | "settled" | "no_action") {
  const life = state === "waiting" ? "WAITING_ENTRY" : state === "entry_failed" ? "ENTRY_FAILED" : state === "settlement_pending" ? "WAITING_SETTLEMENT" : state === "settled" ? "SETTLED" : "NO_ACTION";
  const status = state === "no_action" ? "NO_ACTION" : "RANKED";
  const r1 = state === "no_action" ? null : "spread";
  return {
    notice: "EXPERIMENTAL", decision: decision(), timing_policy_version: "v4-pre-earnings-1530et-v1",
    configurations: [
      cfg("v4_2k_conservative", "2000", "conservative", "300", "NO_ACTION", null, "NO_ACTION"),
      cfg("v4_2k_moderate", "2000", "moderate", "600", status, r1, life),
      cfg("v4_2k_aggressive", "2000", "aggressive", "1000", status, r1, life),
      cfg("v4_10k_conservative", "10000", "conservative", "1500", status, r1, life),
      cfg("v4_10k_moderate", "10000", "moderate", "3000", status, r1, life),
      cfg("v4_10k_aggressive", "10000", "aggressive", "5000", status, r1, life),
    ],
    candidates: [cand("spread", "bull_call_spread", "180"), cand("long_put", "long_put", "1155")],
    default_configuration_key: "v4_2k_moderate",
    entry_observation: state === "waiting" || state === "no_action" ? null : {
      status: state === "entry_failed" ? "NOT_EXECUTABLE" : "OBSERVED", candidate_id: "spread", observed_at: NOW,
      failure_category: state === "entry_failed" ? "REQUIRED_SIDE_QUOTE_MISSING" : null,
      failure_detail: state === "entry_failed" ? "Required ask quote unavailable on leg 0" : null,
      market_data_quality: "delayed", net_executable_value: "-180.00",
    },
    settlement: state === "settled" ? { status: "SETTLED", settled_at: "2026-09-11T19:55:00+00:00", failure_category: null, failure_detail: null, entry_net_value: "-180.00", exit_net_value: "220.00", realized_pnl: "40.00", return_on_standardized_capital: "0.02", market_data_quality: "delayed" } : null,
    settlement_policy: "T+1 at 15:55 ET on the first post-earnings trading day",
  };
}
const fullCandidates = { notice: "EXPERIMENTAL", candidates: [
  { candidate_id: "spread", rank: 1, strategy: "bull_call_spread", expiration: "2026-09-18", geometry_variant_id: null, validity_status: "RANKABLE", status_reason: null,
    semantic: { compatibility: "0.9", tier: "compatible" },
    core: { worst_return: "-0.30", median_return: "0.05", best_return: "0.40", positive_scenario_fraction: "0.57", positive_region_count: 4, region_count: 7, scenarios_valued: 21, no_profitable_region: false, profit_concentrated_in_single_region: false },
    tail_stress: { worst_return: "-0.40", large_move_survival: "0.5", vs_core_worst_delta: "-0.1", scenarios_valued: 12, note: "stress" },
    execution: { mean_relative_spread: "0.05", worst_relative_spread: "0.07", two_sided_leg_count: 2, leg_count: 2, required_sides_complete: true, max_leg_timestamp_skew_seconds: "0", market_data_quality: "delayed" },
    capital: { standardized_capital: "2000", entry_cash_required: "180", capital_utilisation: "0.09" }, rank_explanation: "better downside band",
    scenario_grid: { core: [{ scenario_id: "0_flat", move_label: "0 EM", em_fraction: "0", scenario_underlying_price: "100", iv_label: "flat", iv_multiplier: "1.0", return_executable: "0.05", return_theoretical: "0.05", reason_codes: [] }], stress: [] },
    legs: [{ leg_index: 0, action: "buy", right: "call", strike: "100.000000", quantity: 1, external_contract_id: "91284950", required_side: "ask", required_side_price: "3.20", bid: "3.00", ask: "3.20", implied_volatility: "0.40", market_data_quality: "delayed", source_provider: "ibkr_tws", retrieved_at: NOW }] },
] };

async function mockV4(page: Page, state: Parameters<typeof configurations>[0], withDecision = true) {
  await page.route(`**/v4/shadow/decisions?*`, json({ notice: "EXPERIMENTAL", decisions: withDecision ? [decision()] : [] }));
  await page.route(`**/v4/shadow/decisions`, json({ notice: "EXPERIMENTAL", decisions: withDecision ? [decision()] : [] }));
  await page.route("**/v4/shadow/decisions/7/configurations", json(configurations(state)));
  await page.route("**/v4/shadow/decisions/7/candidates", json(fullCandidates));
}

test.describe("Company workspace (V4-first)", () => {
  test("overview summarises the company with V4 readiness and links deeper", async ({ page }) => {
    await mockV4(page, "waiting");
    await page.goto(`/company/${TICKER}`);
    await expect(page.getByTestId("overview-summary")).toBeVisible({ timeout: 20000 });
    await expect(page.getByTestId("overview-summary")).toContainText("Latest V4 decision");
    await expect(page.getByRole("button", { name: "Earnings Setup", exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "Historical / Control", exact: true })).toBeVisible();
    // Old V3-shaped tabs are gone from the primary flow.
    await expect(page.getByRole("button", { name: "Strategy Lab", exact: true })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "My Exposure" })).toHaveCount(0);
  });

  test("market view separates AI judgment from ranking and labels confidence", async ({ page }) => {
    await mockV4(page, "waiting");
    await page.goto(`/company/${TICKER}`);
    await page.getByRole("button", { name: "Market View", exact: true }).click();
    await expect(page.getByTestId("ai-judgment-notice")).toContainText("AI judgment, not a ranking");
    await expect(page.getByTestId("market-view")).toContainText("NOT A PROBABILITY");
    await expect(page.getByTestId("market-view")).toContainText("BULLISH");
  });

  test("V4 decision tab embeds the six-config lab and candidates tab the explorer", async ({ page }) => {
    await mockV4(page, "waiting");
    await page.goto(`/company/${TICKER}`);
    await page.getByRole("button", { name: "V4 Decision", exact: true }).click();
    await expect(page.getByTestId("config-selector")).toBeVisible();
    await expect(page.getByTestId("config-comparison")).toContainText("$10,000 Aggressive");
    await expect(page.getByTestId("why-this-strategy")).toBeVisible();
    await page.getByRole("button", { name: "Candidates", exact: true }).click();
    await expect(page.getByTestId("candidate-explorer")).toContainText("Bull Call Spread");
  });

  test("legacy on-demand analysis is separated and labelled as non-evidence", async ({ page }) => {
    await mockV4(page, "waiting");
    await page.goto(`/company/${TICKER}`);
    await page.getByRole("button", { name: "Historical / Control", exact: true }).click();
    await expect(page.getByTestId("ondemand-notice")).toContainText("not official forward evidence");
    await expect(page.getByRole("button", { name: "On-demand V3 analysis" })).toBeVisible();
  });

  test("empty V4 state on the company page is honest", async ({ page }) => {
    await mockV4(page, "waiting", false);
    await page.goto(`/company/${TICKER}`);
    await page.getByRole("button", { name: "V4 Decision", exact: true }).click();
    await expect(page.getByText(`No V4 decision for ${TICKER} yet.`)).toBeVisible();
  });
});

test.describe("Forward outcome states", () => {
  for (const [state, expectText, kind] of [
    ["waiting", "Waiting for the entry observation", "warning"],
    ["entry_failed", "Required ask quote unavailable", "failure"],
    ["settlement_pending", "Waiting for post-earnings settlement observation", "warning"],
    ["settled", "Standardized return", "settled"],
    ["no_action", "No candidate satisfied the methodology", "neutral"],
  ] as const) {
    test(`${state} renders its own honest state`, async ({ page }) => {
      await mockV4(page, state);
      await page.goto("/v4-decision-lab/7");
      await expect(page.getByTestId("forward-outcome")).toContainText(expectText);
      if (kind === "failure") {
        await expect(page.getByTestId("failure-explanation")).toContainText("not a losing trade");
        await expect(page.getByTestId("forward-outcome").locator("[data-lifecycle='ENTRY_FAILED']")).toBeVisible();
      }
      if (kind === "settled") {
        await expect(page.getByTestId("forward-outcome")).toContainText("No statistical significance is implied");
        await expect(page.getByTestId("forward-outcome")).toContainText("$40.00");
      }
      if (state === "settlement_pending") {
        await expect(page.getByTestId("forward-outcome")).toContainText("No interim P&L");
      }
      if (kind === "neutral") {
        await expect(page.getByTestId("forward-outcome").locator("[data-lifecycle='NO_ACTION']")).toBeVisible();
      }
    });
  }
  test("six-config comparison shows a lifecycle per configuration", async ({ page }) => {
    await mockV4(page, "settled");
    await page.goto("/v4-decision-lab/7");
    const table = page.getByTestId("config-comparison");
    await expect(table.locator("[data-lifecycle='NO_ACTION']")).toHaveCount(1);
    await expect(table.locator("[data-lifecycle='SETTLED']")).toHaveCount(5);
  });
});
