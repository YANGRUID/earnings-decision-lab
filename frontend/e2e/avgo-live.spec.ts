import { test, expect } from "@playwright/test";

/**
 * Options Decision Engine V3 Part L, scenario 9 -- "AVGO actionable case
 * renders recommendation." Deliberately NOT part of the deterministic
 * CI suite (options-decision-engine.spec.ts): this hits the real dev
 * Docker stack (frontend on :5173, backend on :8000 with a real IBKR
 * connection), matching Part 56's "for local live QA use real IBKR/
 * current DB data." Skipped automatically under CI, and requires the
 * dev Docker containers (see docker compose) to already be running.
 */
test.use({ baseURL: "http://localhost:5173" });

test.skip(!!process.env.CI, "Live IBKR-backed test -- local QA only, never in CI.");

test("AVGO actionable case renders a real recommendation", async ({ page }) => {
  test.slow();
  await page.goto("/company/AVGO");
  await page.getByRole("button", { name: "AI Decision" }).click();

  const marketHeader = page.getByText("Underlying");
  await expect(marketHeader).toBeVisible({ timeout: 15_000 });

  await page.getByRole("button", { name: "Generate New Decision" }).click();
  await expect(page.getByText(/#1 Recommended/)).toBeVisible({ timeout: 60_000 });
  await expect(page.getByText("Model Strategy Score:")).toBeVisible();
});
