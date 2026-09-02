import { test, expect } from "@playwright/test";

/**
 * Options Decision Engine V3 Part L, scenario 9 -- "AVGO actionable case
 * renders recommendation." Deliberately NOT part of the deterministic
 * CI suite (options-decision-engine.spec.ts): this hits the real dev
 * Docker stack (frontend on :5173, backend on :8000 with a real IBKR
 * connection), matching Part 56's "for local live QA use real IBKR/
 * current DB data." Requires the dev Docker containers (see docker
 * compose) to already be running.
 *
 * OPT-IN ONLY (V4.5 final wiring). This used to skip on CI alone, which
 * meant a plain local `npx playwright test` silently ran it against the
 * real stack. That is not a safe default: this spec's second step clicks
 * "Generate New Decision", which writes a REAL V3 decision to the live
 * database and spends live IBKR market-data requests. Run during the
 * 15:55 ET capture window it would contend with the official job.
 *
 * It now runs only when explicitly asked for:
 *
 *   RUN_LIVE_QA=1 npx playwright test e2e/avgo-live.spec.ts
 *
 * Never enable it in CI, and never inside a pre-capture freeze window.
 */
test.use({ baseURL: "http://localhost:5173" });

const LIVE_QA_ENABLED = process.env.RUN_LIVE_QA === "1" && !process.env.CI;

test.skip(
  !LIVE_QA_ENABLED,
  "Live IBKR-backed QA test -- writes a real V3 decision to the live database. " +
    "Opt in explicitly with RUN_LIVE_QA=1, outside any pre-capture freeze window.",
);

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
