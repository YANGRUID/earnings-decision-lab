import { execFileSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

/**
 * Deletes the ZZE2E1 fixture (and everything referencing it) after the
 * suite finishes -- this database is shared with the backend pytest
 * suite (see docs on "edl-test-db"), and at least one pytest test
 * asserts a genuinely empty options-data table system-wide
 * (test_replay_summary_empty_options_data_still_lists_companies); a
 * fixture left behind here breaks that assertion for an unrelated test
 * run. Reuses seed_e2e_fixtures.py's own cleanup logic so there is one
 * real definition of "everything that references this fixture," not two
 * that can drift apart.
 */
export default function globalTeardown(): void {
  const databaseUrl =
    process.env.E2E_DATABASE_URL ??
    "postgresql+psycopg://postgres:change_me@localhost:5434/earnings_decision_lab";
  const backendDir = path.resolve(__dirname, "../../backend");

  execFileSync(
    ".venv/bin/python",
    [
      "-c",
      "from scripts.seed_e2e_fixtures import _clear_existing, TICKER\n" +
        "from db.session import SessionLocal\n" +
        "from models.company import Company\n" +
        "db = SessionLocal()\n" +
        "try:\n" +
        "    company = db.query(Company).filter(Company.ticker == TICKER).one_or_none()\n" +
        "    _clear_existing(db, company)\n" +
        "    db.commit()\n" +
        "finally:\n" +
        "    db.close()\n",
    ],
    {
      cwd: backendDir,
      env: { ...process.env, DATABASE_URL: databaseUrl },
      stdio: "inherit",
    }
  );
}
