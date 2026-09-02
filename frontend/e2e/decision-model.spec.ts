import { test, expect, type Route } from "@playwright/test";

/**
 * V4 DecisionView model provenance (2026-09-02): Operations and Settings
 * show the active model / thinking / reasoning configuration, and a frozen
 * decision shows what actually produced it (configured vs returned model,
 * thinking, effort, tokens, latency). Fixtures only, no backend.
 */

const AI_PROVIDER = {
  state: "green",
  provider: "deepseek",
  configured: true,
  last_successful_generation_at: "2026-09-01T19:55:10Z",
  last_error: null,
  decision_view_model: "deepseek-v4-pro",
  decision_view_thinking: "enabled",
  decision_view_reasoning_effort: "high",
  decision_view_max_tokens: 16384,
  decision_view_config_error: null,
};

async function mockOperations(page: import("@playwright/test").Page, aiProvider = AI_PROVIDER) {
  await page.route("**/operations/summary", (route: Route) =>
    route.fulfill({
      json: {
        health: {
          ibkr: { state: "green", gateway_reachable: true, authenticated: true, connected: true, live_account: true, market_data_quality: "delayed", last_heartbeat_at: "2026-09-02T16:41:55Z", last_error: null, provider: "tws" },
          earnings_calendar: { state: "green", active_provider: "earningsapi", fallback_provider: "finnhub", last_successful_sync_at: "2026-09-02T00:00:00Z", events_received: 3, last_error: null, next_scheduled_sync_at: "2026-09-03T00:00:00Z" },
          ai_provider: aiProvider,
          scheduler: { state: "green", running: true, registered_job_count: 7, last_activity_at: "2026-09-02T16:41:03Z", next_activity_at: "2026-09-02T17:01:17Z" },
          database: { state: "green", backend_healthy: true, database_healthy: true, migration_head: "d2f4b6a81c37" },
        },
        execution_summary: { todays_events: 3, eligibility_passed: 3, eligibility_failed: 0, decisions_created: 0, waiting_for_entry: 0, entries_captured: 0, entry_failures: 0, settlements_due: 0, settled: 0, settlement_failures: 0 },
        official_run: { found: false, run_started_at: null, run_finished_at: null, run_status: null, evaluated: 0, skipped_ineligible: 0, decisions_created: 0, no_action: 0, entries_captured: 0, entries_failed: 0, pipeline_failed: 0, settlements_captured: 0, settlements_failed: 0 },
        preflight: { checks: [], ready: true, blockers: [] },
        market_clock: { utc_now: "2026-09-02T16:45:00Z", new_york_now: "2026-09-02T12:45:00-04:00", zurich_now: "2026-09-02T18:45:00+02:00", market_session: "regular", next_automatic_action_job_id: "ibkr_gateway_healthcheck", next_automatic_action_at: "2026-09-02T17:01:17Z" },
      },
    }),
  );
  await page.route("**/operations/events", (route: Route) => route.fulfill({ json: { events: [] } }));
  await page.route("**/operations/jobs", (route: Route) => route.fulfill({ json: { jobs: [] } }));
  await page.route("**/operations/failures", (route: Route) => route.fulfill({ json: { failures: [] } }));
  await page.route("**/operations/preparation-progress", (route: Route) =>
    route.fulfill({ json: { queue_depth: 0, completed: 0, failed: 0, worker_active: false, current_symbol: null, current_stage: null, step_index: null, step_total: null, attempt: null, heartbeat_seconds_ago: null, elapsed_seconds: null } }),
  );
  await page.route("**/operations/quote-diagnostics/summary", (route: Route) => route.fulfill({ status: 404, json: { detail: "not mocked" } }));
  await page.route("**/v4/shadow/decisions", (route: Route) => route.fulfill({ json: { decisions: [] } }));
}

test.describe("Operations shows the active V4 decision model", () => {
  test("model, thinking and reasoning effort are visible before the first sample", async ({ page }) => {
    await mockOperations(page);
    await page.goto("/operations");
    const block = page.getByTestId("operations-v4-model");
    await expect(block).toBeVisible();
    await expect(block).toContainText("deepseek-v4-pro");
    await expect(block).toContainText("enabled");
    await expect(block).toContainText("high");
    await expect(block).toContainText("16384");
    await expect(page.getByTestId("operations-v4-model-error")).toHaveCount(0);
    // The System card names the V4 view model next to the provider.
    await expect(page.getByText("DeepSeek · V4 view: deepseek-v4-pro · thinking high")).toBeVisible();
  });

  test("a configuration error is shown honestly, never as a fallback model", async ({ page }) => {
    await mockOperations(page, {
      ...AI_PROVIDER,
      decision_view_model: null,
      decision_view_config_error: "V4_DECISION_VIEW_MODEL is not set. There is no fallback to DEEPSEEK_MODEL",
    });
    await page.goto("/operations");
    await expect(page.getByTestId("operations-v4-model")).toContainText("NOT CONFIGURED");
    await expect(page.getByTestId("operations-v4-model-error")).toContainText("no fallback");
    await expect(page.getByTestId("operations-v4-model")).not.toContainText("flash");
  });
});

test.describe("Settings → AI Provider shows the V4 DecisionView model", () => {
  test("renders provider, model, thinking and effort without any key", async ({ page }) => {
    await page.route("**/providers", (route: Route) =>
      route.fulfill({
        json: {
          domains: [
            {
              domain: "llm",
              primary: "deepseek",
              fallback: null,
              primary_is_override: false,
              fallback_is_override: false,
              providers: [
                {
                  provider: "deepseek",
                  domain: "llm",
                  configured: true,
                  masked_key: "••••••••fcc7",
                  last_success_at: "2026-09-02T18:00:50Z",
                  last_error_at: null,
                  last_error_status: null,
                  last_error_detail: null,
                  entitlement_note: null,
                  capabilities: { supports_structured_output: true, supports_tool_calling: true, supports_streaming: true },
                },
              ],
            },
          ],
          strategy_risk_preference: "defined_risk_only",
          v4_decision_view: {
            provider: "deepseek",
            model: "deepseek-v4-pro",
            thinking: "enabled",
            reasoning_effort: "high",
            max_tokens: 16384,
            config_version: "v4-decision-view-model-config-v1",
            config_error: null,
          },
        },
      }),
    );
    await page.goto("/settings/ai-provider");
    const card = page.getByTestId("v4-decision-model-card");
    await expect(card).toBeVisible();
    await expect(page.getByTestId("v4-decision-model")).toHaveText("deepseek-v4-pro");
    await expect(card).toContainText("enabled");
    await expect(card).toContainText("high");
    await expect(card).not.toContainText("sk-");
  });
});
