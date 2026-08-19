/**
 * Deterministic README screenshot capture (Phase 14.10 Part K). Replaces
 * manual OS-level screenshotting -- every run produces the same fixed
 * viewport and only ever captures real, already-rendered page content
 * (Playwright screenshots never include browser chrome or OS decoration).
 * Page-content-only and credential-safe by construction: this script
 * never visits a Settings page, never types into a form, and never reads
 * an environment secret -- it only navigates and clicks tabs on pages
 * that are already provider-key-masked by design (see
 * services/secret_store/masking.py on the backend).
 *
 * Usage:
 *   npm run screenshots
 *
 * Requires a running dev stack (`npm run dev` or the Docker frontend) and
 * a backend with at least one company already researched. Defaults to
 * ticker NVDA; override with SCREENSHOT_TICKER if that ticker has no
 * local research yet. The AI Decision screenshot is only meaningful once
 * that ticker has at least one generated decision -- run one through the
 * UI or `POST /research/{ticker}/decision` first if it's missing.
 */
import { mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium, type Page } from "playwright";

const __dirname = dirname(fileURLToPath(import.meta.url));
const OUT_DIR = process.env.SCREENSHOT_OUT_DIR
  ? resolve(process.env.SCREENSHOT_OUT_DIR)
  : resolve(__dirname, "../../docs/screenshots");
const BASE_URL = process.env.SCREENSHOT_BASE_URL ?? "http://localhost:5173";
const TICKER = (process.env.SCREENSHOT_TICKER ?? "NVDA").toUpperCase();
const VIEWPORT = { width: 1600, height: 1000 };

interface Shot {
  name: string;
  path: string;
  tab?: string;
}

const SHOTS: Shot[] = [
  { name: "home", path: "/" },
  { name: "company_overview", path: `/company/${TICKER}` },
  { name: "upcoming_earnings", path: `/company/${TICKER}`, tab: "Upcoming Earnings" },
  { name: "strategy_lab", path: `/company/${TICKER}`, tab: "Strategy Lab" },
  { name: "earnings_thesis", path: `/company/${TICKER}`, tab: "AI Thesis" },
  { name: "ai_decision", path: `/company/${TICKER}`, tab: "AI Decision" },
  { name: "my_exposure", path: `/company/${TICKER}`, tab: "My Exposure" },
  { name: "history", path: `/company/${TICKER}`, tab: "Historical Events" },
  { name: "ai_research", path: `/research?ticker=${TICKER}` },
  { name: "system_status", path: "/system-status" },
  { name: "cross_company_replay", path: "/historical-replay" },
  { name: "track_record", path: "/track-record" },
];

/** Waits for the page to settle: no in-flight network requests, no visible
 * "Loading…" placeholder, and no residual layout/animation in progress.
 * Deliberately does not key off the empty-state CSS class alone -- that
 * class is reused for genuinely empty (non-loading) real content, which
 * is a valid, honest thing to screenshot. */
async function waitForSettled(page: Page): Promise<void> {
  await page.waitForLoadState("networkidle", { timeout: 20000 }).catch(() => {});
  await page
    .waitForFunction(
      () =>
        !Array.from(document.querySelectorAll(".empty-state")).some((el) =>
          (el.textContent ?? "").includes("Loading")
        ),
      { timeout: 20000 }
    )
    .catch(() => {});
  await page.waitForTimeout(400);
}

async function disableAnimations(page: Page): Promise<void> {
  await page.addStyleTag({
    content: `*, *::before, *::after {
      animation-duration: 0s !important;
      animation-delay: 0s !important;
      transition-duration: 0s !important;
      transition-delay: 0s !important;
      caret-color: transparent !important;
    }`,
  });
}

async function capture(page: Page, shot: Shot): Promise<void> {
  const url = `${BASE_URL}${shot.path}`;
  await page.goto(url, { waitUntil: "domcontentloaded" });
  await disableAnimations(page);
  await waitForSettled(page);

  if (shot.tab) {
    const tabButton = page.locator(`button.tab-button:has-text("${shot.tab}")`);
    if ((await tabButton.count()) === 0) {
      console.warn(`  ⚠ tab "${shot.tab}" not found on ${shot.path} -- skipping ${shot.name}`);
      return;
    }
    await tabButton.first().click();
    await waitForSettled(page);
  }

  const outPath = resolve(OUT_DIR, `${shot.name}.png`);
  await page.screenshot({ path: outPath, fullPage: true });
  console.log(`  ✓ ${shot.name}.png`);
}

async function main(): Promise<void> {
  mkdirSync(OUT_DIR, { recursive: true });
  console.log(`Capturing screenshots from ${BASE_URL} (ticker: ${TICKER})`);

  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: VIEWPORT });

  try {
    for (const shot of SHOTS) {
      await capture(page, shot);
    }
  } finally {
    await browser.close();
  }

  console.log(`Done. Screenshots written to ${OUT_DIR}`);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
