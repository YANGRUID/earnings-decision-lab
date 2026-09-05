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
      }),
      challenger_evidence: {
        historical_move: "READY",
        historical_sample_n: 24,
        historical_timing_quality: "timing_unverified",
        multi_expiry_metadata: "MISSING",
        multi_expiry_replay: "CANNOT_REPLAY_HONESTLY",
        overall: "PARTIAL",
      },
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
      }),
      challenger_evidence: {
        historical_move: "READY",
        historical_sample_n: 14,
        historical_timing_quality: "timing_unverified",
        multi_expiry_metadata: "MISSING",
        multi_expiry_replay: "CANNOT_REPLAY_HONESTLY",
        overall: "PARTIAL",
      },
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
});
