import { defineConfig, devices } from "@playwright/test";

/**
 * V4.1 methodology foundation (2026-08-31) -- AI Earnings Analyst Track
 * Record engine-cohort/capital-terminology coverage. Same rationale as
 * playwright.operations.config.ts: this suite only verifies the page
 * renders real backend response shapes correctly (engine cohort filter,
 * the V3 legacy capital caveat, the standardized per-decision metrics
 * section, the honest V4 zero-data state, and the calibration
 * terminology fix) -- every /benchmark/* call is mocked via page.route()
 * and only the frontend dev server runs. No real backend, no real DB.
 */
const E2E_FRONTEND_PORT = 5183;

export default defineConfig({
  testDir: "./e2e",
  testMatch: "benchmark_track_record.spec.ts",
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  reporter: [["list"]],
  use: {
    baseURL: `http://localhost:${E2E_FRONTEND_PORT}`,
    trace: "retain-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
  webServer: {
    command: `npm run dev -- --port ${E2E_FRONTEND_PORT} --strictPort`,
    url: `http://localhost:${E2E_FRONTEND_PORT}`,
    reuseExistingServer: !process.env.CI,
    timeout: 30_000,
    env: {
      VITE_API_BASE_URL: `http://localhost:${E2E_FRONTEND_PORT}/api/v1`,
    },
  },
});
