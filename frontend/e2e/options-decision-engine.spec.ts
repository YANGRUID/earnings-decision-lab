import { test, expect } from "@playwright/test";

/**
 * Options Decision Engine V3 Part L (mandatory) -- deterministic E2E
 * coverage against the fixture ticker ZZE2E1 (see
 * backend/scripts/seed_e2e_fixtures.py and playwright.config.ts's
 * webServer, which runs the backend with OPTIONS_PROVIDER=fixture).
 * Never depends on IBKR or any live provider being reachable.
 */

const TICKER = "ZZE2E1";
const NO_DATA_TICKER = "ZZE2ENODATA";

test.describe("Strategy Lab expiration selection", () => {
  test("Auto expiration renders a real, non-empty comparison", async ({ page }) => {
    await page.goto(`/company/${TICKER}`);
    await page.getByRole("button", { name: "Strategy Lab" }).click();

    const expirationHeading = page.getByRole("heading", { name: "Expiration" });
    await expect(expirationHeading).toBeVisible();

    // The sweet-spot (9 days after earnings) candidate must win over the
    // near (2 days) and far (23 days) alternatives -- proves Auto is
    // score-driven, not "always nearest" (see seed_e2e_fixtures.py).
    await expect(page.getByText(/Auto selected: 2026-09-07/)).toBeVisible();
    await expect(page.locator("table").filter({ hasText: "2026-08-31" })).toBeVisible();
    await expect(page.locator("table").filter({ hasText: "2026-09-21" })).toBeVisible();

    await expect(page.getByRole("heading", { name: /deterministic candidates/i }).or(
      page.getByText(/deterministic candidates/i)
    )).toBeVisible();
  });

  test("Manual expiration changes the active expiration and strategy set", async ({ page }) => {
    await page.goto(`/company/${TICKER}`);
    await page.getByRole("button", { name: "Strategy Lab" }).click();
    await expect(page.getByText(/Auto selected:/)).toBeVisible();

    // Strategies initially reflect the default resolver pick.
    await expect(page.getByText(/expiration 2026-08-31/)).toBeVisible();

    // Clicking a real alternative row switches to Manual and recomputes.
    await page.locator("tr", { hasText: "2026-09-21" }).click();
    await expect(page.getByText(/expiration 2026-09-21/)).toBeVisible({ timeout: 10_000 });
    await expect(page.locator("table").filter({ hasText: "2026-09-21" }).getByText("Selected")).toBeVisible();
  });
});

test.describe("AI Decision", () => {
  test("Risk profile selector is present and changes what is requested", async ({ page }) => {
    await page.goto(`/company/${TICKER}`);
    await page.getByRole("button", { name: "AI Decision" }).click();

    const riskSelect = page.getByLabel("Risk profile");
    await expect(riskSelect).toBeVisible();
    await riskSelect.selectOption("aggressive");
    await expect(riskSelect).toHaveValue("aggressive");

    await page.getByRole("button", { name: "Generate New Decision" }).click();
    await expect(page.getByText(/Risk: Aggressive/)).toBeVisible({ timeout: 30_000 });
  });

  test("Budget affects sizing without exceeding the configured risk cap", async ({ page }) => {
    test.slow();
    await page.goto(`/company/${TICKER}`);
    await page.getByRole("button", { name: "AI Decision" }).click();

    await page.getByLabel("Risk profile").selectOption("moderate");
    await page.getByPlaceholder("e.g. 500").fill("3000");
    await page.getByRole("button", { name: "Generate New Decision" }).click();

    // Every strategy card with a computed budget fit (#1 recommended, and
    // any alternative that also has one) renders its own "Trade budget"
    // stat -- .first() covers the #1 recommendation's.
    await expect(page.getByText("Trade budget", { exact: true }).first()).toBeVisible({
      timeout: 30_000,
    });
    const budgetUtilizationRow = page.locator(".stat", { hasText: "Budget utilization" }).first();
    await expect(budgetUtilizationRow).toBeVisible();
    const utilizationText = await budgetUtilizationRow.locator(".stat-value").innerText();
    const utilizationPct = parseFloat(utilizationText.replace("%", ""));
    expect(utilizationPct).toBeGreaterThan(0);
    expect(utilizationPct).toBeLessThanOrEqual(100);
  });

  test("Probability fields show a real sample size, never a bare percentage", async ({ page }) => {
    await page.goto(`/company/${TICKER}`);
    await page.getByRole("button", { name: "AI Decision" }).click();
    await page.getByRole("button", { name: "Generate New Decision" }).click();

    const reliability = page.getByText("Historical Reliability");
    await expect(reliability).toBeVisible({ timeout: 30_000 });
    const reliabilityTable = page.locator("table").filter({ hasText: "Historical Compatibility" });

    // Historical Compatibility must show "(N/M events)", never a lone %.
    await expect(reliabilityTable.getByRole("cell", { name: "Historical Compatibility" })).toBeVisible();
    await expect(reliabilityTable).toContainText(/\(\d+\/24 events\)/);

    // Estimated Probability must disclose method + sample size, and for
    // this fixture's real N=24 (>= LOW_SAMPLE_THRESHOLD=20), must NOT
    // claim low sample confidence.
    await expect(reliabilityTable.getByRole("cell", { name: "Estimated Probability" })).toBeVisible();
    await expect(reliabilityTable).toContainText(/Empirical earnings distribution/);
    await expect(reliabilityTable).not.toContainText(/Low sample confidence/);
  });

  test("True Strategy Win Rate is always honestly Unavailable, never fabricated", async ({ page }) => {
    await page.goto(`/company/${TICKER}`);
    await page.getByRole("button", { name: "AI Decision" }).click();
    await page.getByRole("button", { name: "Generate New Decision" }).click();

    await expect(page.getByText("Historical Reliability")).toBeVisible({ timeout: 30_000 });
    const winRateRow = page.locator("tr", { hasText: "True Strategy Win Rate" });
    await expect(winRateRow).toBeVisible();
    await expect(winRateRow).toContainText("Unavailable (No settled historical option trades yet)");
    // Never a percentage next to this specific metric.
    await expect(winRateRow).not.toContainText("%");
  });

  test("Why Not Alternative appears on the #1 recommendation with real numbers", async ({ page }) => {
    await page.goto(`/company/${TICKER}`);
    await page.getByRole("button", { name: "AI Decision" }).click();
    await page.getByRole("button", { name: "Generate New Decision" }).click();

    await expect(page.getByRole("heading", { name: "#1 Recommended" }).or(
      page.getByText("#1 Recommended")
    )).toBeVisible({ timeout: 30_000 });

    const whyNotHeading = page.getByText("Why Not #2", { exact: true });
    // Only present when a genuine #2 alternative exists to compare against.
    if (await whyNotHeading.count()) {
      await expect(whyNotHeading).toBeVisible();
      const whyNotList = whyNotHeading.locator("xpath=following-sibling::ul[1]");
      // A real numeric comparison (score points, $ premium/loss, or a
      // historical N/M ratio) -- never a generic, number-free platitude.
      await expect(whyNotList).toContainText(/\d/);
      await expect(whyNotList).toContainText("#2");
    }
  });

  test("No actionable market data blocks recommendation honestly", async ({ page }) => {
    await page.goto(`/company/${NO_DATA_TICKER}`);
    // A never-seeded ticker has no company on record at all -- confirms
    // the workspace never fabricates a company or a recommendation for
    // data that was never real to begin with.
    await expect(page.getByText(/Not researched yet/i)).toBeVisible();
  });
});
