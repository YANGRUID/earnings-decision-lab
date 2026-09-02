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
          database: { state: "green", backend_healthy: true, database_healthy: true, migration_head: "f4b6d8e0c2a3" },
          v4_shadow: { state: "green", enabled: true, decisions_today: 0, ranked_today: 0, no_action_today: 0, failed_today: 0, entry_observations_failed_today: 0, settlements_due: 0, settlements_complete: 0, last_run_at: null, engine_version: "options-decision-engine-v4", decision_time_et: "15:30", settlement_time_et: "15:30", timing_policy_version: "v4-1530-entry-1530-t1-settlement-v2", note: "V4 forward test" },
        },
        today: { decision_window_et: "15:30", settlement_window_et: "15:30", deadline_et: "15:50", events_in_window: 1, business_eligible: 1, research_ready: 1, waiting_decision: 1, decisions_today: 0, ranked_today: 0, no_action_today: 0, entries_observed_today: 0, entries_failed_today: 0, deadline_skipped_today: 0, research_not_ready_today: 0, settlements_due_today: 0, settled_today: 0, settlements_failed_today: 0 },
        readiness: { window_days: 7, upcoming_events: 1, business_eligible: 1, company_resolved: 1, research_queued: 0, research_running: 0, research_ready: 1, research_failed: 0, ai_thesis_ready: 1, v4_decision_ready: 1, next_window_at: "2026-09-03T19:30:00Z", next_window_ready: 1, next_window_total: 1 },
        staleness: [],
        preflight: { checks: [], ready: true, blockers: [] },
        market_clock: { utc_now: "2026-09-02T16:45:00Z", new_york_now: "2026-09-02T12:45:00-04:00", zurich_now: "2026-09-02T18:45:00+02:00", market_session: "regular", next_automatic_action_job_id: "ibkr_gateway_healthcheck", next_automatic_action_at: "2026-09-02T17:01:17Z", settlement_window_tolerance_minutes: 5 },
      },
    }),
  );
  await page.route("**/operations/events", (route: Route) => route.fulfill({ json: { events: [] } }));
  await page.route("**/operations/jobs", (route: Route) => route.fulfill({ json: { jobs: [] } }));
  await page.route("**/operations/failures", (route: Route) => route.fulfill({ json: { failures: [] } }));
  await page.route("**/operations/preparation-progress", (route: Route) =>
    route.fulfill({ json: { queue_depth: 0, completed: 0, failed: 0, worker_active: false, current_symbol: null, current_stage: null, step_index: null, step_total: null, attempt: null, heartbeat_seconds_ago: null, elapsed_seconds: null } }),
  );
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
