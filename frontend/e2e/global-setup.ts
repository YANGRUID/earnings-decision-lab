import { execFileSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

// Where seed_e2e_fixtures.py writes the *actual* dates it computed
// (relative to "today" -- see that script's own docstring on why). Spec
// files import fixtureDatesPath (below) and read this file themselves
// rather than ever hardcoding an absolute date -- a hardcoded date only
// matches on the day someone happened to run the suite and silently
// breaks every day after (confirmed live: exactly what broke this suite
// before this existed).
export const fixtureDatesPath = path.resolve(__dirname, ".fixture-dates.json");

/**
 * Seeds the deterministic ZZE2E1 fixture ticker (see
 * backend/scripts/seed_e2e_fixtures.py) into the disposable E2E Postgres
 * database before any test runs. Runs once per full `playwright test`
 * invocation, before webServer processes are started for the first
 * test, so the backend under test finds real, already-seeded rows on
 * its first request -- never a race with an empty database.
 */
export default function globalSetup(): void {
  const databaseUrl =
    process.env.E2E_DATABASE_URL ??
    "postgresql+psycopg://postgres:change_me@localhost:5434/earnings_decision_lab";
  const backendDir = path.resolve(__dirname, "../../backend");

  execFileSync(".venv/bin/python", ["-m", "scripts.seed_e2e_fixtures"], {
    cwd: backendDir,
    env: {
      ...process.env,
      DATABASE_URL: databaseUrl,
      E2E_FIXTURE_DATES_PATH: fixtureDatesPath,
    },
    stdio: "inherit",
  });
}
