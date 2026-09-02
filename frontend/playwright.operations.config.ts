import { defineConfig, devices } from "@playwright/test";

/**
 * Pre-live hardening (2026-08-25) Section 8 -- Live Operations Monitor
 * smoke coverage. Same rationale as playwright.ibkr.config.ts: this
 * suite only verifies the page renders real backend response shapes
 * correctly (healthy/critical states, scheduler job rows, pipeline
 * lifecycle rows, no mutation controls, navigation to the company
 * workspace) -- it doesn't need a real backend or database, so every
 * /operations/* call is mocked via page.route() and only the frontend
 * dev server runs. No real IBKR/EarningsAPI/LLM call, no real DB.
 */
const E2E_FRONTEND_PORT = 5182;

export default defineConfig({
  testDir: "./e2e",
  testMatch: "operations.spec.ts",
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
      // Same-origin as the page itself -- page.route()'s mocked
      // responses would otherwise be blocked by the browser's own CORS
      // enforcement, since a real backend never runs in this suite.
      VITE_API_BASE_URL: `http://localhost:${E2E_FRONTEND_PORT}/api/v1`,
    },
  },
});
