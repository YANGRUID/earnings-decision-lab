/**
 * Deterministic V3.0.0 release screenshot capture -- follows the same
 * pattern as capture_screenshots.ts (real page content only, animations
 * disabled, no OS-level screenshotting), but targets specific real UI
 * regions the V3 release notes reference rather than whole pages, so
 * each image shows exactly one feature.
 *
 * Usage:
 *   npx tsx scripts/capture_v3_screenshots.ts
 *
 * Requires a running dev stack with a ticker that already has a real
 * Strategy Lab chain and at least one generated AI Decision (defaults to
 * AVGO; override with SCREENSHOT_TICKER).
 */
import { mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium, type Page } from "playwright";

const __dirname = dirname(fileURLToPath(import.meta.url));
const OUT_DIR = process.env.SCREENSHOT_OUT_DIR
  ? resolve(process.env.SCREENSHOT_OUT_DIR)
  : resolve(__dirname, "../../docs/images");
const BASE_URL = process.env.SCREENSHOT_BASE_URL ?? "http://localhost:5173";
const TICKER = (process.env.SCREENSHOT_TICKER ?? "AVGO").toUpperCase();
const VIEWPORT = { width: 1600, height: 1000 };

async function waitForSettled(page: Page): Promise<void> {
  await page.waitForLoadState("networkidle", { timeout: 20000 }).catch(() => {});
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

async function clickTab(page: Page, tab: string): Promise<void> {
  await page.locator(`button.tab-button:has-text("${tab}")`).first().click();
  await waitForSettled(page);
}

async function main(): Promise<void> {
  mkdirSync(OUT_DIR, { recursive: true });
  console.log(`Capturing V3 release screenshots from ${BASE_URL} (ticker: ${TICKER})`);

  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: VIEWPORT });

  try {
    // 1. Strategy Lab expiration ranking. Requires a live options provider
    // (not the persisted resolver fallback the rest of Strategy Lab uses)
    // -- skip cleanly, never fabricate, if it's genuinely unavailable
    // right now (e.g. the local IBKR Gateway session isn't authenticated).
    await page.goto(`${BASE_URL}/company/${TICKER}`, { waitUntil: "domcontentloaded" });
    await disableAnimations(page);
    await waitForSettled(page);
    await clickTab(page, "Strategy Lab");
    try {
      // A real, live multi-expiration IBKR scan (3 expirations, fetched
      // and scored sequentially) has been observed taking up to ~16s --
      // generous on purpose rather than treating normal live latency as
      // "unavailable".
      await page.getByText(/Auto selected:/).waitFor({ timeout: 45000 });
      const expirationCard = page.locator(".card", { hasText: "Expiration" }).first();
      await expirationCard.screenshot({
        path: resolve(OUT_DIR, "strategy_lab_expiration_ranking.png"),
      });
      console.log("  ✓ strategy_lab_expiration_ranking.png");
    } catch {
      console.warn(
        "  ⚠ Expiration comparison card did not render (live options provider unavailable " +
          "right now) -- skipped strategy_lab_expiration_ranking.png, run again once it is."
      );
    }

    // 2/3/4. AI Decision tab: risk profile controls, the #1 recommendation,
    // and the probability/explanation region, all from the same generated
    // decision so the three images are internally consistent with each
    // other.
    await clickTab(page, "AI Decision");
    await page.getByText("Current View").waitFor({ timeout: 20000 });

    const controlsCard = page.locator(".card", { hasText: "Risk profile" }).first();
    await controlsCard.screenshot({ path: resolve(OUT_DIR, "risk_profile_selection.png") });
    console.log("  ✓ risk_profile_selection.png");

    const recommendationCard = page.locator(".strategy-card").first();
    await recommendationCard.screenshot({ path: resolve(OUT_DIR, "ai_decision_recommendation.png") });
    console.log("  ✓ ai_decision_recommendation.png");

    // Everything from "Historical Reliability" down to the bottom of the
    // #1 card -- there is no tighter DOM wrapper around just probability +
    // why-bullets (they're siblings inside the same strategy-card as the
    // legs table/score breakdown above them), so this clips a vertical
    // slice of that card rather than isolating a distinct element. Uses
    // the card's own x/width (not the viewport's) so the clip never bleeds
    // into the sidebar to its left. A plain `clip` only ever captures what
    // Chromium has actually painted within the current viewport, so the
    // viewport is temporarily grown tall enough to fit the whole card
    // in one frame -- otherwise the clip silently truncates at the
    // original viewport's bottom edge instead of reaching the real card
    // bottom (confirmed live: exactly what happened before this was
    // added).
    const tallViewport = { width: VIEWPORT.width, height: 3200 };
    await page.setViewportSize(tallViewport);
    await waitForSettled(page);
    const reliabilityHeading = page.getByText("Historical Reliability", { exact: true });
    const startBox = await reliabilityHeading.boundingBox();
    const cardBox = await recommendationCard.boundingBox();
    if (startBox && cardBox) {
      await page.screenshot({
        path: resolve(OUT_DIR, "probability_explanation.png"),
        clip: {
          x: cardBox.x,
          y: startBox.y - 12,
          width: cardBox.width,
          height: cardBox.y + cardBox.height - (startBox.y - 12) - 12,
        },
      });
      console.log("  ✓ probability_explanation.png");
    } else {
      console.warn("  ⚠ could not locate probability/explanation region -- skipped");
    }
    await page.setViewportSize(VIEWPORT);

    // 5. Track Record page.
    await page.goto(`${BASE_URL}/track-record`, { waitUntil: "domcontentloaded" });
    await disableAnimations(page);
    await waitForSettled(page);
    await page.screenshot({ path: resolve(OUT_DIR, "track_record.png"), fullPage: true });
    console.log("  ✓ track_record.png");
  } finally {
    await browser.close();
  }

  console.log(`Done. Screenshots written to ${OUT_DIR}`);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
