import { test, expect, type Route } from "@playwright/test";

/**
 * Phase 4.8A -- Settings -> Interactive Brokers' "Connect IBKR" workflow.
 * Every backend response here is a deliberately-chosen, hand-labeled
 * fixture (CONNECTED / AUTH_REQUIRED / GATEWAY_UNREACHABLE) via
 * page.route() -- this suite never talks to a real Gateway or a real
 * IBKR account, and never pretends one connected successfully. See
 * playwright.ibkr.config.ts for why this is a separate, backend-free
 * config from the rest of this project's E2E suite.
 */

const BASE_SYSTEM_STATUS = {
  counts: {
    companies: 0,
    earnings_events: 0,
    earnings_events_with_results: 0,
    price_bars: 0,
    filings: 0,
    document_chunks: 0,
    earnings_estimate_snapshots: 0,
    options_snapshots: 0,
    volatility_snapshots: 0,
  },
  freshness: {
    latest_price_bar_date: null,
    latest_filing_retrieved_at: null,
    latest_earnings_estimate_snapshot_at: null,
    latest_options_snapshot_at: null,
  },
  llm: { provider: "deepseek", model: null, configured: false },
  embedding_model: "test-embedding-model",
  evaluation: null,
  market_session: "closed",
  providers: { domains: [] },
};

function ibkrStatus(overrides: Record<string, unknown> = {}) {
  return {
    gateway_reachable: true,
    authenticated: true,
    connected: true,
    competing: false,
    error: null,
    status_label: "CONNECTED",
    ...overrides,
  };
}

// IBKR TWS Migration, Phase 3 readiness (Section 41) -- the TWS-transport
// sibling of ibkrStatus above (that one, the existing Web/Client-Portal-
// Gateway fixture, is unchanged). configured: false by default -- every
// existing test in this file that doesn't pass a tws override keeps
// getting the exact same "Web is active" shape it always has.
function twsStatus(overrides: Record<string, unknown> = {}) {
  return {
    configured: false,
    gateway_reachable: false,
    socket_connected: false,
    api_ready: false,
    market_data_quality: null,
    error: null,
    status_label: "NOT_CONFIGURED",
    last_heartbeat: null,
    reconnect_state: "disconnected",
    ...overrides,
  };
}

async function mockSystemStatus(
  route: Route,
  ibkr: Record<string, unknown>,
  tws: Record<string, unknown> = twsStatus()
) {
  await route.fulfill({ json: { ...BASE_SYSTEM_STATUS, ibkr, tws } });
}

test.describe("Settings -> Interactive Brokers", () => {
  test("renders the Connected status", async ({ page }) => {
    await page.route("**/system-status", (route) => mockSystemStatus(route, ibkrStatus()));

    await page.goto("/settings/ibkr");

    await expect(page.getByText("🟢 Connected")).toBeVisible();
    await expect(page.getByText("IBKR: CONNECTED")).toBeVisible();
  });

  test("renders Authentication Required when the session isn't authenticated", async ({
    page,
  }) => {
    await page.route("**/system-status", (route) =>
      mockSystemStatus(
        route,
        ibkrStatus({ authenticated: false, connected: false, status_label: "AUTH_REQUIRED" })
      )
    );

    await page.goto("/settings/ibkr");

    await expect(page.getByText("🔴 Authentication Required")).toBeVisible();
  });

  test("renders Gateway Offline, and the Connect IBKR / Refresh Status buttons exist", async ({
    page,
  }) => {
    await page.route("**/system-status", (route) =>
      mockSystemStatus(
        route,
        ibkrStatus({
          gateway_reachable: false,
          authenticated: false,
          connected: false,
          error: "could not reach the IBKR Client Portal Gateway",
          status_label: "GATEWAY_UNREACHABLE",
        })
      )
    );

    await page.goto("/settings/ibkr");

    await expect(page.getByText("⚪ Gateway Offline")).toBeVisible();
    await expect(page.getByRole("button", { name: "Connect IBKR" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Refresh Status" })).toBeVisible();
  });

  test("clicking Connect IBKR requests the gateway URL from the backend and opens it", async ({
    page,
    context,
  }) => {
    await page.route("**/system-status", (route) => mockSystemStatus(route, ibkrStatus()));
    let connectRequested = false;
    await page.route("**/ibkr/connect", async (route) => {
      connectRequested = true;
      // Never a password, session token, or account identifier -- exactly
      // the shape GET /ibkr/connect actually returns (schemas/api.py::
      // IbkrConnectResponse).
      await route.fulfill({ json: { url: "https://localhost:5000" } });
    });
    // context.route (not page.route): the popup below is a NEW page on
    // this same context, and nothing real listens on :5000 in this test
    // environment. This stubs the destination itself reachable so the
    // navigation actually commits and its URL is observable -- it does
    // not fabricate anything about a real IBKR session (the real Gateway
    // login page is never rendered, proxied, or asserted on by this
    // app, live or in this test).
    await context.route("https://localhost:5000/**", (route) =>
      route.fulfill({ body: "<html><body>Gateway login (test stub)</body></html>", contentType: "text/html" })
    );

    await page.goto("/settings/ibkr");

    const [popup] = await Promise.all([
      context.waitForEvent("page"),
      page.getByRole("button", { name: "Connect IBKR" }).click(),
    ]);

    expect(connectRequested).toBe(true);
    await expect.poll(() => popup.url()).toBe("https://localhost:5000/");
  });

  test("Refresh Status re-fetches system status without a fake successful session", async ({
    page,
  }) => {
    let callCount = 0;
    await page.route("**/system-status", (route) => {
      callCount += 1;
      // Genuinely unreachable both times -- refreshing must never
      // fabricate a connected state on its own; it only re-asks the
      // backend, which (in this test) honestly keeps reporting offline.
      return mockSystemStatus(
        route,
        ibkrStatus({
          gateway_reachable: false,
          authenticated: false,
          connected: false,
          status_label: "GATEWAY_UNREACHABLE",
        })
      );
    });

    await page.goto("/settings/ibkr");
    await expect(page.getByText("⚪ Gateway Offline")).toBeVisible();
    const callsAfterLoad = callCount;

    await page.getByRole("button", { name: "Refresh Status" }).click();

    await expect.poll(() => callCount).toBeGreaterThan(callsAfterLoad);
    await expect(page.getByText("⚪ Gateway Offline")).toBeVisible();
  });
});

// --------------------------------------------------------------------------
// IBKR TWS Migration, Phase 3 readiness (Section 41) -- provider-aware
// rendering when ibkr_provider=tws is configured. Every backend response
// here is a hand-labeled fixture, exactly like the Web suite above --
// this suite never talks to a real IB Gateway either.
// --------------------------------------------------------------------------

test.describe("Settings -> Interactive Brokers (TWS transport)", () => {
  test("TWS READY: no Connect IBKR button, shows provider and market data quality", async ({
    page,
  }) => {
    await page.route("**/system-status", (route) =>
      mockSystemStatus(
        route,
        ibkrStatus(),
        twsStatus({
          configured: true,
          gateway_reachable: true,
          socket_connected: true,
          api_ready: true,
          market_data_quality: "delayed",
          status_label: "CONNECTED",
          reconnect_state: "ready",
        })
      )
    );

    await page.goto("/settings/ibkr");

    await expect(page.getByText("🟢 Ready")).toBeVisible();
    await expect(page.getByText("Provider: IB Gateway / TWS API")).toBeVisible();
    await expect(page.getByText("delayed")).toBeVisible();
    await expect(page.getByRole("button", { name: "Connect IBKR" })).not.toBeVisible();
    await expect(page.getByText("https://localhost:5001")).not.toBeVisible();
  });

  test("TWS AUTH REQUIRED: shows manual IB Gateway login instructions, no browser-login button", async ({
    page,
  }) => {
    await page.route("**/system-status", (route) =>
      mockSystemStatus(
        route,
        ibkrStatus(),
        twsStatus({
          configured: true,
          gateway_reachable: true,
          socket_connected: true,
          api_ready: false,
          status_label: "AUTH_REQUIRED",
          reconnect_state: "connected",
        })
      )
    );

    await page.goto("/settings/ibkr");

    await expect(page.getByText("🔴 Authentication required")).toBeVisible();
    await expect(page.getByText("IB Gateway login required.")).toBeVisible();
    await expect(page.getByText("Open IB Gateway on this Mac")).toBeVisible();
    await expect(page.getByRole("button", { name: "Connect IBKR" })).not.toBeVisible();
  });

  test("TWS DISCONNECTED: gateway unreachable renders as Disconnected", async ({ page }) => {
    await page.route("**/system-status", (route) =>
      mockSystemStatus(
        route,
        ibkrStatus(),
        twsStatus({
          configured: true,
          gateway_reachable: false,
          socket_connected: false,
          api_ready: false,
          status_label: "GATEWAY_UNREACHABLE",
          error: "could not reach IB Gateway/TWS",
          reconnect_state: "disconnected",
        })
      )
    );

    await page.goto("/settings/ibkr");

    await expect(page.getByText("⚪ Disconnected")).toBeVisible();
    await expect(page.getByText("could not reach IB Gateway/TWS")).toBeVisible();
  });

  test("TWS RECONNECTING: shown as its own distinct state, not lumped into offline", async ({
    page,
  }) => {
    await page.route("**/system-status", (route) =>
      mockSystemStatus(
        route,
        ibkrStatus(),
        twsStatus({
          configured: true,
          gateway_reachable: true,
          socket_connected: false,
          api_ready: false,
          status_label: "GATEWAY_UNREACHABLE",
          reconnect_state: "reconnecting",
        })
      )
    );

    await page.goto("/settings/ibkr");

    await expect(page.getByText("🟡 Reconnecting")).toBeVisible();
  });

  test("Web rollback: with tws not configured, the Web card and Connect IBKR still render unchanged", async ({
    page,
  }) => {
    await page.route("**/system-status", (route) => mockSystemStatus(route, ibkrStatus()));

    await page.goto("/settings/ibkr");

    await expect(page.getByText("🟢 Connected")).toBeVisible();
    await expect(page.getByText("IBKR: CONNECTED")).toBeVisible();
    await expect(page.getByRole("button", { name: "Connect IBKR" })).toBeVisible();
    await expect(page.getByText("Provider: IB Gateway / TWS API")).not.toBeVisible();
  });
});
