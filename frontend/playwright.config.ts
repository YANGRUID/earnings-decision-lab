import { defineConfig, devices } from "@playwright/test";

/**
 * Options Decision Engine V3 Part L -- deterministic E2E coverage.
 *
 * Both the frontend and backend under test are spun up fresh by
 * webServer below, pointed at the disposable E2E Postgres database and
 * a fixture options provider (OPTIONS_PROVIDER=fixture, see
 * backend/src/providers/fixture_options.py) -- this suite never touches
 * IBKR or any live market-data provider, so it's safe to run in CI with
 * no network dependency beyond localhost. globalSetup seeds the
 * deterministic ZZE2E1 fixture ticker before any test runs (see
 * backend/scripts/seed_e2e_fixtures.py).
 *
 * E2E_DATABASE_URL must point at a disposable/test Postgres -- never a
 * real deployment's database. Defaults to this project's standard local
 * disposable instance (see docs on the "edl-test-db" container).
 */
const E2E_BACKEND_PORT = 8011;
const E2E_FRONTEND_PORT = 5180;
const E2E_DATABASE_URL =
  process.env.E2E_DATABASE_URL ??
  "postgresql+psycopg://postgres:change_me@localhost:5434/earnings_decision_lab";

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  reporter: [["list"]],
  globalSetup: "./e2e/global-setup.ts",
  globalTeardown: "./e2e/global-teardown.ts",
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
  webServer: [
    {
      command: `cd ../backend && DATABASE_URL="${E2E_DATABASE_URL}" OPTIONS_PROVIDER=fixture .venv/bin/python -m uvicorn api.main:app --port ${E2E_BACKEND_PORT}`,
      url: `http://localhost:${E2E_BACKEND_PORT}/api/v1/health`,
      reuseExistingServer: !process.env.CI,
      timeout: 30_000,
    },
    {
      command: `npm run dev -- --port ${E2E_FRONTEND_PORT} --strictPort`,
      url: `http://localhost:${E2E_FRONTEND_PORT}`,
      reuseExistingServer: !process.env.CI,
      timeout: 30_000,
      env: {
        VITE_API_BASE_URL: `http://localhost:${E2E_BACKEND_PORT}/api/v1`,
      },
    },
  ],
});
