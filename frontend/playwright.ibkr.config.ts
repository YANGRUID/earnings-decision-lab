import { defineConfig, devices } from "@playwright/test";

/**
 * Phase 4.8A -- IBKR "Connect" workflow coverage (Settings -> Interactive
 * Brokers). Deliberately its own config, separate from playwright.config.ts:
 * that suite boots a real backend against a disposable Postgres to exercise
 * the actual Options Decision Engine, which this test doesn't need at all --
 * every backend call this spec touches (/system-status, /ibkr/connect) is
 * mocked via page.route(), matching the explicit instruction to never
 * fabricate a real, successful IBKR session. No database, no backend
 * process, and -- critically -- no real network call to IBKR or to any
 * Gateway is ever made by this suite; only the frontend dev server runs.
 */
const E2E_FRONTEND_PORT = 5181;

export default defineConfig({
  testDir: "./e2e",
  testMatch: "ibkr-connect.spec.ts",
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
      // enforcement (route.fulfill() doesn't set Access-Control-Allow-
      // Origin by default), since a real backend never runs in this
      // suite for it to come from.
      VITE_API_BASE_URL: `http://localhost:${E2E_FRONTEND_PORT}/api/v1`,
    },
  },
});
