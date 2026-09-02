import { test, expect, type Route } from "@playwright/test";

/**
 * V4.1 methodology foundation (2026-08-31) -- AI Earnings Analyst Track
 * Record smoke coverage: engine cohort filtering, the V3 legacy capital
 * caveat, the standardized per-decision metrics section, the honest V4
 * zero-data state, and the calibration terminology fix (forensic audit
 * Part I Section 12). Every backend response here is a deliberately
 * hand-labeled fixture matching the real schemas
 * (backend/src/schemas/api.py::BenchmarkTrackRecordResponse /
 * BenchmarkCalibrationResponse) via page.route() -- this suite never
 * talks to a real backend/database, and never fabricates a real
 * DecisionSnapshot/EntrySnapshot/SettlementSnapshot. See
 * playwright.trackrecord.config.ts for why this is its own,
 * backend-free config, same pattern as playwright.operations.config.ts.
 */

function v3TrackRecord(overrides: Record<string, unknown> = {}) {
  return {
    portfolio_id: 1,
    total_decisions: 19,
    actionable_decisions: 16,
    no_action_decisions: 3,
    entries_captured: 9,
    entries_capture_failed: 7,
    settled_decisions: 7,
    win_rate: { correct: 0, total: 7, pct: "0" },
    average_r: "-2.43",
    median_r: "-1.55",
    expectancy: "-2.43",
    profit_factor: "0",
    max_drawdown: "9215.00",
    max_drawdown_pct: "460.8",
    directional_accuracy: { correct: 0, total: 0, pct: null },
    breakeven_accuracy: { correct: 1, total: 5, pct: "0.2" },
    range_accuracy: { correct: 3, total: 6, pct: "0.5" },
    legacy_capital_caveat:
      "This portfolio drawdown figure aggregates decisions that each independently sized " +
      "against the full $2,000 BenchmarkPortfolio capital (V3's real sizing behavior -- that " +
      "capital was never actually shared or depleted across concurrent positions), not a true " +
      "portfolio equity curve. Not comparable to a real portfolio drawdown. See the " +
      "standardized, per-decision metrics for a correctly-labeled reading of the same real " +
      "settlements.",
    standardized: {
      n: 7,
      wins: 0,
      losses: 7,
      mean_return_on_standardized_capital: "-0.4436",
      median_return_on_standardized_capital: "-0.4305",
      total_realized_pnl: "-9215.00",
      portfolio_drawdown_available: false,
      portfolio_drawdown_reason:
        "No true portfolio simulator exists in this codebase (no shared capital " +
        "reservation, no concurrency accounting, no cash debit/credit on entry/exit). Each " +
        "decision below used its own independent $2,000 standardized capital; summing their " +
        "real dollar losses against one shared $2,000 base, the way V3's legacy figure does, " +
        "is not a valid portfolio drawdown.",
    },
    ...overrides,
  };
}

function v4EmptyTrackRecord() {
  return {
    portfolio_id: 1,
    total_decisions: 0,
    actionable_decisions: 0,
    no_action_decisions: 0,
    entries_captured: 0,
    entries_capture_failed: 0,
    settled_decisions: 0,
    win_rate: { correct: 0, total: 0, pct: null },
    average_r: null,
    median_r: null,
    expectancy: null,
    profit_factor: null,
    max_drawdown: null,
    max_drawdown_pct: null,
    directional_accuracy: { correct: 0, total: 0, pct: null },
    breakeven_accuracy: { correct: 0, total: 0, pct: null },
    range_accuracy: { correct: 0, total: 0, pct: null },
    legacy_capital_caveat: null,
    standardized: {
      n: 0,
      wins: 0,
      losses: 0,
      mean_return_on_standardized_capital: null,
      median_return_on_standardized_capital: null,
      total_realized_pnl: "0",
      portfolio_drawdown_available: false,
      portfolio_drawdown_reason:
        "No true portfolio simulator exists in this codebase (no shared capital " +
        "reservation, no concurrency accounting, no cash debit/credit on entry/exit).",
    },
  };
}

function calibration() {
  return {
    portfolio_id: 1,
    settled_decisions: 7,
    buckets: [
      { label: "<60%", lower: null, upper: 60, rate: { correct: 0, total: 0, pct: null } },
      { label: "60-70%", lower: 60, upper: 70, rate: { correct: 0, total: 0, pct: null } },
      { label: "70-80%", lower: 70, upper: 80, rate: { correct: 0, total: 1, pct: "0" } },
      { label: "80-90%", lower: 80, upper: 90, rate: { correct: 0, total: 3, pct: "0" } },
      { label: "90%+", lower: 90, upper: null, rate: { correct: 0, total: 1, pct: "0" } },
    ],
  };
}

async function mockTrackRecordApi(
  page: import("@playwright/test").Page,
  { trackRecordByEngine }: { trackRecordByEngine: Record<string, Record<string, unknown>> }
) {
  await page.route("**/benchmark/track-record*", (route: Route) => {
    const url = new URL(route.request().url());
    const engine = url.searchParams.get("engine_version") ?? "";
    const body = trackRecordByEngine[engine] ?? trackRecordByEngine[""];
    route.fulfill({ json: body });
  });
  await page.route("**/benchmark/calibration*", (route: Route) =>
    route.fulfill({ json: calibration() })
  );
}

test.describe("AI Earnings Analyst Track Record", () => {
  test("defaults to All Engines and shows the V3 legacy caveat and standardized metrics", async ({
    page,
  }) => {
    await mockTrackRecordApi(page, {
      trackRecordByEngine: { "": v3TrackRecord() },
    });

    await page.goto("/benchmark-track-record");

    await expect(page.getByRole("heading", { name: "V3 Legacy Aggregate Loss" })).toHaveCount(0);
    await expect(page.getByText("V3 Legacy Aggregate Loss")).toBeVisible();
    await expect(page.getByText(/Not comparable as a portfolio drawdown percentage/)).toBeVisible();
    await expect(page.getByText(/never actually shared or depleted/)).toBeVisible();

    await expect(page.getByRole("heading", { name: "Standardized Per-Decision Metrics" })).toBeVisible();
    await expect(page.getByText("Not Available")).toBeVisible();
    await expect(page.getByText(/No true portfolio simulator exists/)).toBeVisible();
  });

  test("selecting the V4 cohort shows the honest zero-data state, never a fabricated row", async ({
    page,
  }) => {
    await mockTrackRecordApi(page, {
      trackRecordByEngine: {
        "": v3TrackRecord(),
        "options-decision-engine-v4": v4EmptyTrackRecord(),
      },
    });

    await page.goto("/benchmark-track-record");
    await page.getByRole("combobox").first().selectOption("options-decision-engine-v4");

    await expect(page.getByText(/V4 has zero official decisions today/)).toBeVisible();
    await expect(page.getByText(/experimental and disabled for official trading/)).toBeVisible();
  });

  test("switching back to V3 shows real settled performance again", async ({ page }) => {
    await mockTrackRecordApi(page, {
      trackRecordByEngine: {
        "": v3TrackRecord(),
        "options-decision-engine-v3": v3TrackRecord(),
        "options-decision-engine-v4": v4EmptyTrackRecord(),
      },
    });

    await page.goto("/benchmark-track-record");
    await page.getByRole("combobox").first().selectOption("options-decision-engine-v4");
    await expect(page.getByText(/V4 has zero official decisions today/)).toBeVisible();

    await page.getByRole("combobox").first().selectOption("options-decision-engine-v3");
    await expect(page.getByRole("heading", { name: "Performance Metrics" })).toBeVisible();
    await expect(page.getByText("V3 Legacy Aggregate Loss")).toBeVisible();
  });

  test("calibration terminology no longer claims to be a probability calibration", async ({
    page,
  }) => {
    await mockTrackRecordApi(page, {
      trackRecordByEngine: { "": v3TrackRecord() },
    });

    await page.goto("/benchmark-track-record");

    await expect(
      page.getByRole("heading", { name: "Historical Compatibility vs. Realized Outcome" })
    ).toBeVisible();
    await expect(page.getByRole("heading", { name: "Probability Calibration" })).toHaveCount(0);
    await expect(page.getByText("Predicted probability")).toHaveCount(0);
    await expect(page.getByText("Historical Move Compatibility")).toBeVisible();
    await expect(page.getByText("This is not a calibrated probability of profit.")).toBeVisible();
  });
});
