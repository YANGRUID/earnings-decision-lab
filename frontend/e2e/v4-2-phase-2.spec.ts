import { expect, test } from "@playwright/test";

// V4.2 PHASE 2 -- the independent-search surface.
//
// The properties pinned here are the ones a reader would be misled by if they
// broke: all six configurations are always shown, even when they agree; the
// number of DISTINCT structures they chose is stated, because that is what
// says whether they decided separately; a refusal names its binding
// constraint; and an empty page says "has not run", never "found nothing".

const json = (body: unknown) => async (route: import("@playwright/test").Route) =>
  route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });

const CONFIGS = [
  { configuration_key: "v4_2k_conservative", label: "$2,000 Conservative", capital_base: "2000", risk_profile: "conservative", max_risk_dollars: "300", max_risk_utilization_pct: "15", min_bid_ask_coverage: "0.80" },
  { configuration_key: "v4_2k_moderate", label: "$2,000 Moderate", capital_base: "2000", risk_profile: "moderate", max_risk_dollars: "600", max_risk_utilization_pct: "30", min_bid_ask_coverage: "0.40" },
  { configuration_key: "v4_2k_aggressive", label: "$2,000 Aggressive", capital_base: "2000", risk_profile: "aggressive", max_risk_dollars: "1000", max_risk_utilization_pct: "50", min_bid_ask_coverage: null },
  { configuration_key: "v4_10k_conservative", label: "$10,000 Conservative", capital_base: "10000", risk_profile: "conservative", max_risk_dollars: "1500", max_risk_utilization_pct: "15", min_bid_ask_coverage: "0.80" },
  { configuration_key: "v4_10k_moderate", label: "$10,000 Moderate", capital_base: "10000", risk_profile: "moderate", max_risk_dollars: "3000", max_risk_utilization_pct: "30", min_bid_ask_coverage: "0.40" },
  { configuration_key: "v4_10k_aggressive", label: "$10,000 Aggressive", capital_base: "10000", risk_profile: "aggressive", max_risk_dollars: "5000", max_risk_utilization_pct: "50", min_bid_ask_coverage: null },
];

const STATUS = {
  notice: "V4.2 PHASE 2 -- INDEPENDENT SEARCH. A separate methodology from Phase 1.",
  methodology_version: "v4.2-independent-search-v1",
  versions: { methodology_version: "v4.2-independent-search-v1" },
  enabled: true,
  activation_at: "2026-09-25T19:30:00+00:00",
  would_evaluate_a_window_now: true,
  activation_state: "ACTIVE",
  max_expiries: 3,
  configurations: CONFIGS,
  recorded: {
    decisions: 1,
    candidates: 38,
    configuration_results: 6,
    configurations_actioned: 5,
    events_with_divergent_selections: 1,
    expiries_searched: 3,
  },
};

const census = (over: Record<string, number> = {}) => ({
  universe_count: 38,
  data_invalid_count: 0,
  strategy_not_permitted_count: 0,
  liquidity_rejected_count: 0,
  economic_rejected_count: 16,
  move_edge_rejected_count: 4,
  capital_rejected_count: 0,
  risk_rejected_count: 0,
  rankable_count: 18,
  ...over,
});

const config = (key: string, over: Record<string, unknown> = {}) => ({
  configuration_key: key,
  capital_base: key.includes("10k") ? "10000" : "2000",
  risk_profile: key.split("_").pop(),
  max_risk_dollars: "600",
  status: "ACTION",
  selected_candidate_id: "put_credit_spread:WIDER@2026-10-16",
  rank: 1,
  quantity: 3,
  capital_used: "540",
  max_risk_used: "540",
  reason: "$2,000 Moderate ranked 18 eligible structures and took put credit spread.",
  rejection_summary: census(),
  ranked_candidate_ids: ["put_credit_spread:WIDER@2026-10-16"],
  ranking_version: "v4_2_per_configuration_ranking_v1",
  ...over,
});

const EVENT = {
  decision_id: 42,
  ticker: "PHTWO",
  observed_at: "2026-09-25T19:30:00+00:00",
  status: "ACTION",
  expiries_considered: 3,
  multi_expiry_status: "MULTI_EXPIRY",
  candidates_evaluated: 38,
  configurations_actioned: 5,
  distinct_selected_candidates: 2,
  market_data_requests: 8,
  unique_contracts_quoted: 19,
  total_latency_ms: "2400.0",
  failure_category: null,
  configurations: [
    config("v4_2k_conservative", {
      status: "NO_ACTION",
      selected_candidate_id: null,
      rank: null,
      quantity: null,
      capital_used: null,
      max_risk_used: null,
      max_risk_dollars: "300",
      reason:
        "$2,000 Conservative: none of 38 candidates was eligible (most common refusal RISK_CAP_EXCEEDED, 15 of them). one contract risks $850, above $2,000 Conservative's $300.00 cap",
      rejection_summary: census({
        strategy_not_permitted_count: 18,
        risk_rejected_count: 15,
        economic_rejected_count: 2,
        move_edge_rejected_count: 3,
        rankable_count: 0,
      }),
      ranked_candidate_ids: [],
    }),
    config("v4_2k_moderate", { selected_candidate_id: "bear_put_spread:NARROW@2026-09-25" }),
    config("v4_2k_aggressive"),
    config("v4_10k_conservative", { max_risk_dollars: "1500" }),
    config("v4_10k_moderate", { max_risk_dollars: "3000" }),
    config("v4_10k_aggressive", { max_risk_dollars: "5000" }),
  ],
};

const candidate = (over: Record<string, unknown> = {}) => ({
  candidate_id: "put_credit_spread:WIDER@2026-10-16",
  strategy: "put_credit_spread",
  expiration: "2026-10-16",
  expiry_ladder_position: 2,
  entry_dte: 21,
  dte_at_settlement: 20,
  settlement_risk: "clear",
  expiry_implied_move_pct: "0.0410",
  geometry_variant_id: "WIDER",
  validity_status: "RANKABLE",
  validity_reason: "fully valued",
  core_median_return: "0.0620",
  core_worst_return: "-0.1800",
  core_best_return: "0.3100",
  core_positive_scenario_fraction: "0.6100",
  move_edge_status: "NOT_APPLICABLE",
  move_edge_exposure: null,
  mean_relative_spread: "0.0420",
  entry_cash_required: "0",
  per_contract_max_risk: "180",
  n_legs: 2,
  n_legs_with_two_sided_quote: 2,
  rankable_somewhere: true,
  market_data_quality: "delayed",
  ...over,
});

const DETAIL = {
  notice: STATUS.notice,
  decision_id: 42,
  ticker: "PHTWO",
  observed_at: "2026-09-25T19:30:00+00:00",
  methodology_version: "v4.2-independent-search-v1",
  versions: { candidate_universe: "v4_2_independent_universe_v1" },
  evidence: {
    underlying_price: "100",
    market_data_quality: "delayed",
    implied_move_pct: "0.0620",
    historical_sample_n: 30,
    historical_evidence_quality: "adequate",
    historical_median_abs_move_pct: "0.0137",
  },
  request_budget: {
    stages: [
      { stage: "underlying", requests: 1, contracts: 0, latency_ms: "12" },
      { stage: "metadata", requests: 1, contracts: 0, latency_ms: "40" },
      { stage: "chain_discovery", requests: 3, contracts: 44, latency_ms: "900" },
      { stage: "quotes", requests: 3, contracts: 19, latency_ms: "1400" },
    ],
    total_requests: 8,
    unique_contracts: 19,
    contracts_deduplicated: 46,
    contracts_reused_from_control: 6,
    challenger_only_contracts: 13,
    total_latency_ms: "2400",
  },
  expiries_considered: 3,
  multi_expiry_status: "MULTI_EXPIRY",
  candidates: [
    candidate(),
    candidate({
      candidate_id: "bear_put_spread:NARROW@2026-09-25",
      strategy: "bear_put_spread",
      expiration: "2026-09-25",
      expiry_ladder_position: 0,
      expiry_implied_move_pct: "0.0620",
      entry_cash_required: "250",
      per_contract_max_risk: "250",
    }),
    candidate({
      candidate_id: "long_call:ATM@2026-10-16",
      strategy: "long_call",
      core_median_return: "-0.1200",
      rankable_somewhere: false,
      validity_status: "RANKABLE",
      entry_cash_required: "4200",
      per_contract_max_risk: "4200",
    }),
  ],
  configurations: EVENT.configurations,
};

test.beforeEach(async ({ page }) => {
  await page.route("**/v4-2/challenger/phase2/status", json(STATUS));
  await page.route("**/v4-2/challenger/phase2/decisions", json({ notice: STATUS.notice, events: [EVENT] }));
  await page.route("**/v4-2/challenger/phase2/decisions/42", json(DETAIL));
});

test.describe("V4.2 Phase 2", () => {
  test("shows all six configurations even where they agree", async ({ page }) => {
    await page.goto("/challenger-phase-2");
    const grid = page.getByTestId("phase2-config-grid");
    for (const key of CONFIGS.map((c) => c.configuration_key)) {
      await expect(page.getByTestId(`phase2-config-${key}`)).toBeVisible();
    }
    await expect(grid).toContainText("$2,000 Conservative");
    await expect(grid).toContainText("$10,000 Aggressive");
  });

  test("states how many distinct structures the six chose", async ({ page }) => {
    await page.goto("/challenger-phase-2");
    const event = page.getByTestId("phase2-event-PHTWO");
    await expect(event).toContainText("Distinct structures");
    await expect(event).toContainText("configurations diverged");
  });

  test("a declining configuration names its binding constraint", async ({ page }) => {
    await page.goto("/challenger-phase-2");
    const row = page.getByTestId("phase2-config-v4_2k_conservative");
    await expect(row).toContainText("No action");
    await expect(row).toContainText("Above risk cap");
    await expect(page.getByTestId("phase2-event-PHTWO")).toContainText(
      "one contract risks $850",
    );
  });

  test("no-action is presented as an outcome, never as a failure", async ({ page }) => {
    await page.goto("/challenger-phase-2");
    const row = page.getByTestId("phase2-config-v4_2k_conservative");
    await expect(row).not.toContainText(/failed/i);
    await expect(row).not.toContainText(/error/i);
  });

  test("the candidate explorer shows refused candidates too, and filters", async ({ page }) => {
    await page.goto("/challenger-phase-2");
    const table = page.getByTestId("phase2-candidates");
    await expect(table).toContainText("long call");
    await expect(table).toContainText("put credit spread");

    await page.getByTestId("filter-rankable").selectOption("rejected");
    await expect(table).toContainText("long call");
    await expect(table).not.toContainText("put credit spread");
  });

  test("filtering to one configuration shows that configuration's eligible set", async ({
    page,
  }) => {
    await page.goto("/challenger-phase-2");
    await page.getByTestId("filter-config").selectOption("v4_2k_conservative");
    // Conservative ranked nothing on this event, so its eligible set is empty.
    await expect(page.getByTestId("phase2-candidates")).not.toContainText("put credit spread");
  });

  test("an expiry filter narrows the universe to one rung", async ({ page }) => {
    await page.goto("/challenger-phase-2");
    const table = page.getByTestId("phase2-candidates");
    await page.getByTestId("filter-expiry").selectOption("2026-09-25");
    await expect(table).toContainText("bear put spread");
    await expect(table).not.toContainText("long call");
  });

  test("an empty Phase 2 says it has not run, not that it found nothing", async ({ page }) => {
    await page.route(
      "**/v4-2/challenger/phase2/status",
      json({ ...STATUS, recorded: { ...STATUS.recorded, decisions: 0 } }),
    );
    await page.route(
      "**/v4-2/challenger/phase2/decisions",
      json({ notice: STATUS.notice, events: [] }),
    );
    await page.goto("/challenger-phase-2");
    const empty = page.getByTestId("phase2-empty");
    await expect(empty).toContainText("has not yet run a live window");
    await expect(empty).toContainText("not that it found nothing");
  });

  test("the comparison page keeps the three methodologies separate", async ({ page }) => {
    await page.route(
      "**/v4-2/challenger/comparison",
      json({ notice: "comparison", counts: { events: 0, challenger_evaluated: 0, differs: 0 }, events: [] }),
    );
    await page.route(
      "**/v4-2/challenger/phase2/status",
      json({ ...STATUS, recorded: { ...STATUS.recorded, decisions: 0 } }),
    );
    await page.goto("/methodology-comparison");
    const panel = page.getByTestId("methodology-boundaries");
    await expect(panel).toContainText("V4.1 Control");
    await expect(panel).toContainText("V4.2 Phase 1");
    await expect(panel).toContainText("V4.2 Phase 2");
    await expect(panel).toContainText("N = 0");
    await expect(panel).not.toContainText(/better than/i);
  });
});
