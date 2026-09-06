import { expect, test } from "@playwright/test";

// The V4.2 challenger research surface. These pin the properties that keep it
// honest: neutral language, ex-ante only, and a visible reason whenever the
// challenger declines.

const json = (body: unknown) => async (route: import("@playwright/test").Route) =>
  route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });

const side = (over: Record<string, unknown> = {}) => ({
  methodology: "V4.1 CONTROL",
  status: "RANKED",
  selected_candidate_id: "iron_condor:x",
  strategy: "iron_condor",
  expiration: "2026-09-18",
  median_return: -0.0822,
  worst_return: -0.2563,
  positive_scenario_fraction: 0.1428,
  no_action_reason: null,
  candidates_evaluated: 17,
  candidates_accepted: 17,
  move_edge_status: null,
  move_edge_ratio: null,
  expiry_ladder_position: null,
  entry_dte: null,
  dte_at_settlement: null,
  lifecycle: null,
  ...over,
});

const COMPARISON = {
  notice:
    "V4.1 CONTROL vs V4.2 CHALLENGER -- methodology comparison, not a verdict. V4.2 is not production and has placed nothing.",
  counts: { events: 2, challenger_evaluated: 2, differs: 2 },
  events: [
    {
      ticker: "GWRE",
      earnings_calendar_event_id: 1,
      observed_at: "2026-09-03T19:30:00+00:00",
      control: side(),
      challenger: side({
        methodology: "V4.2 CHALLENGER",
        selected_candidate_id: "call_credit_spread:x",
        strategy: "call_credit_spread",
        median_return: 0.0421,
        worst_return: -0.2726,
        positive_scenario_fraction: 0.5714,
        candidates_accepted: 2,
        move_edge_status: "EDGE_CONFIRMED",
        move_edge_ratio: 1.42,
        expiry_ladder_position: 1,
        entry_dte: 8,
        dte_at_settlement: 7,
        lifecycle: {
          state: "WAITING_SETTLEMENT",
          entries_observed: 4,
          entries_failed: 0,
          settled: 0,
          settlement_failed: 0,
          settlement_grades: [],
          realized_pnl: null,
        },
      }),
      challenger_evidence: {
        historical_move: "READY",
        historical_sample_n: 24,
        historical_timing_quality: "timing_unverified",
        multi_expiry_metadata: "MISSING",
        multi_expiry_replay: "CANNOT_REPLAY_HONESTLY",
        overall: "PARTIAL",
      },
      multi_expiry: [
        {
          expiration: "2026-09-11",
          ladder_position: 0,
          entry_dte: 1,
          dte_at_settlement: 0,
          settlement_risk: "EXPIRES_ON_SETTLEMENT_DATE",
          implied_move_pct: "0.041",
          candidates: 9,
          viable_candidates: 0,
          best_median_return: null,
          best_worst_return: null,
          best_candidate_id: null,
          mean_relative_spread: null,
          move_edge_status: "NO_EDGE",
        },
        {
          expiration: "2026-09-18",
          ladder_position: 1,
          entry_dte: 8,
          dte_at_settlement: 7,
          settlement_risk: "EXPIRES_WITHIN_A_WEEK_OF_SETTLEMENT",
          implied_move_pct: "0.062",
          candidates: 11,
          viable_candidates: 2,
          best_median_return: "0.0421",
          best_worst_return: "-0.2726",
          best_candidate_id: "call_credit_spread:x",
          mean_relative_spread: "0.081",
          move_edge_status: "EDGE_CONFIRMED",
        },
      ],
      configurations: [
        {
          configuration_key: "v4_2k_conservative",
          control_status: "RANKED",
          control_candidate_id: "iron_condor:x",
          challenger_status: "NO_ACTION",
          challenger_candidate_id: null,
          challenger_no_action_reason: "CAPITAL_INCOMPATIBLE (1)",
        },
      ],
      differs: true,
    },
    {
      ticker: "ZS",
      earnings_calendar_event_id: 2,
      observed_at: "2026-09-03T19:36:00+00:00",
      control: side({ strategy: "iron_butterfly", selected_candidate_id: "iron_butterfly:x" }),
      challenger: side({
        methodology: "V4.2 CHALLENGER",
        status: "NO_ACTION",
        selected_candidate_id: null,
        strategy: null,
        expiration: null,
        median_return: null,
        worst_return: null,
        positive_scenario_fraction: null,
        no_action_reason:
          "no candidate cleared the absolute economic viability gate: NEGATIVE_MEDIAN_EXECUTABLE_RETURN (14), NO_PROFITABLE_REGION (2)",
        candidates_accepted: 0,
        lifecycle: {
          state: "NO_ACTION",
          entries_observed: 0,
          entries_failed: 0,
          settled: 0,
          settlement_failed: 0,
          settlement_grades: [],
          realized_pnl: null,
        },
      }),
      challenger_evidence: {
        historical_move: "READY",
        historical_sample_n: 14,
        historical_timing_quality: "timing_unverified",
        multi_expiry_metadata: "MISSING",
        multi_expiry_replay: "CANNOT_REPLAY_HONESTLY",
        overall: "PARTIAL",
      },
      multi_expiry: [],
      configurations: [],
      differs: true,
    },
  ],
};

test.beforeEach(async ({ page }) => {
  await page.route("**/v4-2/challenger/comparison", json(COMPARISON));
});

test.describe("V4.2 methodology comparison", () => {
  test("presents both sides without claiming either is better", async ({ page }) => {
    await page.goto("/methodology-comparison");
    await expect(page.getByTestId("challenger-notice")).toContainText("not a verdict");
    const gwre = page.getByTestId("comparison-GWRE");
    await expect(gwre.getByTestId("side-control")).toContainText("V4.1 CONTROL");
    await expect(gwre.getByTestId("side-challenger")).toContainText("V4.2 CHALLENGER");
    await expect(page.locator("body")).not.toContainText(/\b(better|winner|improved|beats)\b/i);
  });

  test("shows the challenger's refusal reason prominently", async ({ page }) => {
    await page.goto("/methodology-comparison");
    const zs = page.getByTestId("comparison-ZS");
    await expect(zs.getByTestId("no-action-challenger")).toContainText("NO ACTION");
    await expect(zs.getByTestId("no-action-challenger")).toContainText("NO_PROFITABLE_REGION");
  });

  test("reports multi-expiry replay honestly for events with no frozen chain", async ({ page }) => {
    await page.goto("/methodology-comparison");
    await expect(page.getByTestId("comparison-GWRE")).toContainText("CANNOT_REPLAY_HONESTLY");
  });

  test("surfaces configurations where the two methodologies disagree", async ({ page }) => {
    await page.goto("/methodology-comparison");
    const gwre = page.getByTestId("comparison-GWRE");
    await expect(gwre).toContainText("$2,000 Conservative");
    await expect(gwre).toContainText("CAPITAL_INCOMPATIBLE");
  });

  test("shows no realized outcome before settlement", async ({ page }) => {
    await page.goto("/methodology-comparison");
    await expect(page.locator("body")).not.toContainText(/realized p&l/i);
    await expect(page.locator("body")).not.toContainText(/\$[0-9,]+ profit/i);
  });

  test("shows the bounded expiry ladder with per-expiry economics", async ({ page }) => {
    await page.goto("/methodology-comparison");
    const ladder = page.getByTestId("comparison-GWRE").getByTestId("expiry-ladder");
    await expect(ladder).toContainText("2026-09-11");
    await expect(ladder).toContainText("2026-09-18");
    // Each rung carries its OWN implied move, never the nearest expiry's.
    await expect(ladder).toContainText("4.10%");
    await expect(ladder).toContainText("6.20%");
    await expect(ladder).toContainText("expires that day");
  });

  test("labels a no-action decision as intentional rather than failed", async ({ page }) => {
    await page.goto("/methodology-comparison");
    const zs = page.getByTestId("comparison-ZS");
    await expect(zs.getByTestId("lifecycle-challenger")).toContainText("No action");
    await expect(zs.getByTestId("lifecycle-challenger")).not.toContainText(/failed/i);
  });

  test("shows a frozen position awaiting settlement without inventing a result", async ({
    page,
  }) => {
    await page.goto("/methodology-comparison");
    const gwre = page.getByTestId("comparison-GWRE");
    await expect(gwre.getByTestId("lifecycle-challenger")).toContainText(
      "Awaiting T+1 settlement",
    );
    await expect(gwre.getByTestId("lifecycle-challenger")).not.toContainText(/realized/i);
  });
});
