import { expect, test } from "@playwright/test";

// The V4.2 CHALLENGER forward track record. These pin the properties that stop
// a challenger cohort quietly becoming a claim: it is labelled experimental, it
// is never merged with the V4.1 record, NO ACTION reads as intentional, an
// end-of-day mark is never presented as a fill, and a tiny sample always says
// so.

const json = (body: unknown) => async (route: import("@playwright/test").Route) =>
  route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });

const TRACK_RECORD = {
  notice:
    "V4.2 CHALLENGER -- PARALLEL SHADOW, EXPERIMENTAL FORWARD EVIDENCE. A separate cohort from the V4.1 Track Record, never merged into it.",
  methodology: "CHALLENGER",
  cohort: "v4_2_parallel_shadow",
  events: {
    observed: 3,
    action: 1,
    no_action: 2,
    failed: 0,
    action_rate: 0.3333,
    no_action_reasons: {
      "no candidate cleared the absolute economic viability gate": 2,
    },
  },
  lifecycle: {
    entries_observed: 4,
    entries_failed: 0,
    settlements_due: 2,
    settled: 2,
    settlement_failed: 0,
  },
  all_outcomes: {
    settled: 2,
    wins: 1,
    losses: 1,
    flat: 0,
    win_rate: 0.5,
    median_standardized_return: "0.0042",
    median_capital_used_return: "0.0181",
    total_realized_pnl: "17.50",
  },
  executable_only_outcomes: {
    settled: 1,
    wins: 1,
    losses: 0,
    flat: 0,
    win_rate: 1.0,
    median_standardized_return: "0.0119",
    median_capital_used_return: "0.0402",
    total_realized_pnl: "42.00",
  },
  settlement_quality: {
    EXECUTABLE_BID_ASK: 1,
    MARKET_CLOSE_FALLBACK: 1,
    EXPIRATION_INTRINSIC_AT_CLOSE: 0,
    UNRESOLVED: 0,
  },
  by_configuration: {
    v4_2k_conservative: { evaluated: 3, action: 0, no_action: 3, settled: 0 },
    v4_10k_moderate: {
      evaluated: 3,
      action: 1,
      no_action: 2,
      settled: 2,
      wins: 1,
      losses: 1,
      median_standardized_return: "0.0042",
    },
  },
  by_strategy: { call_credit_spread: { settled: 2, wins: 1, losses: 1 } },
  by_expiry_ladder_position: { "1": { settled: 2, wins: 1, losses: 1 } },
  warnings: [
    "3 natural event(s) observed. Every rate below describes what happened, and none of them supports an inference about what will happen.",
    "Some outcomes were priced at an end-of-day closing mark or expiration intrinsic value rather than an executable bid/ask. Those are not fills; the executable-only view excludes them.",
  ],
};

const OPERATIONS = {
  notice: "V4.2 CHALLENGER -- PARALLEL SHADOW, EXPERIMENTAL FORWARD EVIDENCE.",
  scheduler: {
    parallel_enabled: false,
    state: "disabled",
    phase: "challenger",
    runs_inside: "v4_forward_window",
    separate_job_registered: false,
    control_priority: true,
  },
  counts: {
    events_evaluated: 3,
    action: 1,
    no_action: 2,
    entry_observed: 4,
    entry_failed: 0,
    settlement_due: 2,
    settled: 2,
    settlement_failed: 0,
    evaluation_failed: 0,
  },
  no_action_is_a_failure: false,
  affects_v4_1_readiness: false,
};

test.beforeEach(async ({ page }) => {
  await page.route("**/v4-2/challenger/track-record", json(TRACK_RECORD));
  await page.route("**/v4-2/challenger/operations", json(OPERATIONS));
});

test.describe("V4.2 challenger track record", () => {
  test("is labelled a challenger and never described as better", async ({ page }) => {
    await page.goto("/challenger-track-record");
    await expect(page.getByTestId("challenger-label")).toContainText("PARALLEL SHADOW");
    await expect(page.getByTestId("challenger-notice")).toContainText("separate cohort");
    await expect(page.locator("body")).not.toContainText(/\b(better|winner|improved|beats)\b/i);
  });

  test("names V4.1 as the official methodology", async ({ page }) => {
    await page.goto("/challenger-track-record");
    await expect(page.locator("body")).toContainText("V4.1 CONTROL");
    await expect(page.locator("body")).toContainText("Official methodology");
  });

  test("reports the event as the primary unit, configurations separately", async ({ page }) => {
    await page.goto("/challenger-track-record");
    await expect(page.locator("body")).toContainText(
      "six sizings of the same forecast, not six forecasts",
    );
    await expect(page.locator("body")).toContainText("By configuration");
  });

  test("presents no action as a successful outcome with its reason", async ({ page }) => {
    await page.goto("/challenger-track-record");
    await expect(page.locator("body")).toContainText(
      "NO ACTION is a successful methodology outcome",
    );
    await expect(page.locator("body")).toContainText(
      "no candidate cleared the absolute economic viability gate",
    );
  });

  test("separates executable outcomes from end-of-day marks", async ({ page }) => {
    await page.goto("/challenger-track-record");
    await expect(page.getByTestId("outcomes-all-outcomes")).toContainText("2");
    await expect(page.getByTestId("outcomes-executable-only")).toContainText("1");
    await expect(page.locator("body")).toContainText("A closing mark is not a fill");
    await expect(page.locator("body")).toContainText("End-of-day closing mark");
  });

  test("always carries a tiny-sample warning", async ({ page }) => {
    await page.goto("/challenger-track-record");
    await expect(page.getByTestId("tiny-n-warnings")).toContainText("3 natural event(s) observed");
    await expect(page.getByTestId("tiny-n-warnings")).toContainText(
      "none of them supports an inference about what will happen",
    );
  });

  test("shows the parallel flag state without implying V4.2 is live product", async ({ page }) => {
    await page.goto("/challenger-track-record");
    await expect(page.locator("body")).toContainText("DISABLED");
    await expect(page.locator("body")).toContainText("A phase of the V4.1 window");
  });
});
