from datetime import datetime
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from models.enums import MarketDataQualityPolicy

# .env lives at the project root (backend/src/core/config.py -> backend/src ->
# backend -> root), not wherever the process happens to be run from —
# resolving it relative to this file means `.env` loads correctly regardless
# of cwd (e.g. running scripts from `backend/` vs. the repo root).
_PROJECT_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_PROJECT_ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    app_env: str = "development"
    log_level: str = "INFO"

    # IBKR TWS Migration, post-cutover cleanup (2026-09-01) -- opt-in gate
    # for the read-only in-process TWS diagnostic (api/routers/
    # tws_diagnostics.py). Deliberately its OWN flag rather than reusing
    # ``app_env != "production"`` (which already gates admin/v4-
    # experimental): that default is "development", so an app_env gate
    # would leave this endpoint exposed by default in this very
    # deployment, which is exactly what post-cutover cleanup asked to
    # stop. Default False means the normal production API surface does
    # not carry it at all -- the route isn't even registered, so it never
    # appears in /docs and cannot be reached, rather than being
    # registered-then-refused.
    enable_internal_diagnostics: bool = False

    # V4.4C (2026-09-01) -- master switch for V4 SHADOW forward-test
    # generation. Default False, and it must stay False until shadow
    # activation is explicitly authorized: with this off, no shadow
    # scheduler job is registered, no shadow decision is ever written,
    # and the official V3 path is byte-for-byte unaffected. V4 remains
    # experimental regardless of this flag -- turning it on produces
    # SHADOW evidence in separate tables, never official V3 evidence,
    # and never a brokerage order.
    v4_shadow_enabled: bool = False

    database_url: str = (
        "postgresql+psycopg://postgres:change_me@localhost:5433/earnings_decision_lab"
    )

    tiingo_api_key: str | None = None
    alpha_vantage_api_key: str | None = None
    options_data_api_key: str | None = None

    # Phase 4 -- the forward-looking, cross-symbol earnings calendar's
    # FALLBACK source (see providers/finnhub.py) -- EarningsAPI.com is now
    # primary (earningsapi_api_key below); Finnhub's own free tier was
    # confirmed live to return far-future placeholder dates instead of
    # erroring, which this fallback ordering exists specifically to work
    # around. See EARNINGS_CALENDAR_PROVIDER_ARCHITECTURE_REVIEW.md.
    # Deliberately separate from alpha_vantage_api_key: the existing
    # per-ticker "next earnings date" flow (services/market_expectations.py)
    # is untouched and keeps using Alpha Vantage.
    finnhub_api_key: str | None = None

    # The forward-looking, cross-symbol earnings calendar's PRIMARY source
    # (see providers/earningsapi.py). Free tier: 100 req/day, 1000/month --
    # services/earnings_calendar_sync.py's own per-date dedup keeps real
    # daily usage to roughly 1-3 requests in steady state, well under both
    # limits. Get a free key at earningsapi.com.
    earningsapi_api_key: str | None = None

    # --- Options-chain provider (provider-agnostic — see providers/base.py
    # and providers/factory.py) ---
    options_provider: str = "alpha_vantage"

    # IBKR Client Portal Gateway (Phase 13). Runs locally on the user's own
    # machine (see docs/ibkr_integration.md) -- this project never talks to
    # IBKR's cloud directly, only to a Gateway instance already
    # authenticated by the user themselves outside this codebase. Read-only:
    # no order-execution endpoint is ever called against this URL.
    ibkr_base_url: str = "https://localhost:5001/v1/api"

    # Phase 4.8A -- the ibkr-gateway container's host-published port (see
    # docker-compose.yml's ibkr-gateway service and .env.example's
    # IBKR_GATEWAY_PORT). Used only by GET /ibkr/connect to construct the
    # browser-facing login URL (api/routers/ibkr.py) -- distinct from
    # ibkr_base_url above, which is the BACKEND CONTAINER's own path to
    # the same Gateway via host.docker.internal. A browser running on the
    # operator's own machine must use localhost; host.docker.internal only
    # resolves inside another container.
    ibkr_gateway_port: int = 5000

    # IBKR TWS Migration Phase 1 (provider architecture + read-only parity
    # foundation) -- selects which real IBKR transport a constructed
    # "ibkr" options provider actually uses (see providers/factory.py).
    # Deliberately SEPARATE from options_provider above: that field
    # chooses IBKR vs. a different data vendor entirely; this one chooses
    # which of IBKR's own two APIs to speak once "ibkr" is chosen. Only
    # "web" (the existing Client Portal Gateway / IBeam integration,
    # providers/ibkr_options.py -- UNCHANGED by this migration) and "tws"
    # (the new IB Gateway / TWS socket API, providers/ibkr_tws_options.py)
    # are recognized. Defaults to "web" -- this migration's own Phase 1
    # rule is that no existing deployment's behavior changes unless this
    # is explicitly set; the official scheduler stays on "web" throughout
    # Phase 1 regardless of this setting (see services/scheduler.py --
    # nothing there reads ibkr_provider yet).
    ibkr_provider: str = "web"

    # IB Gateway / TWS's own socket API (distinct from ibkr_base_url's
    # HTTPS REST Gateway above) -- reachable only when ibkr_provider=tws.
    # host.docker.internal is the correct default for the same reason
    # ibkr_base_url's own comment explains: IB Gateway/TWS's GUI and 2FA
    # requirements make it substantially simpler to run on the operator's
    # own host machine than to containerize (see this migration's own
    # Phase 1 report, Section P) -- the backend container reaches it the
    # same way it already reaches a host-run Client Portal Gateway.
    #
    # Well-known IBKR default ports (never assumed without being stated
    # here explicitly -- see this migration's Phase 1 report, Section D):
    # IB Gateway live=4001, IB Gateway paper=4002, TWS live=7496,
    # TWS paper=7497. This project defaults to 4002 (IB Gateway PAPER) --
    # a deliberately safe default for a brand-new, unauthenticated-by-
    # default integration; switching to a live port is an explicit,
    # informed operator choice, not something this migration silently
    # opts a fresh deployment into.
    ibkr_tws_host: str = "host.docker.internal"
    ibkr_tws_port: int = 4002

    # A fixed, deterministic per-service identifier IB Gateway/TWS uses to
    # distinguish simultaneous API connections -- never random (see this
    # migration's Phase 1 report, Section K, client ID strategy). Phase 1
    # runs a single provider connection (backend); a future service that
    # also connects (e.g. research-worker) must be assigned its own
    # distinct fixed ID here, documented, never left to chance -- IB
    # Gateway/TWS rejects a second connection that reuses an already-
    # active client ID (error 326) rather than silently sharing it.
    ibkr_tws_client_id: int = 101

    # --- LLM provider (provider-agnostic — see docs/llm_providers.md) ---
    llm_provider: str = "deepseek"

    deepseek_api_key: str | None = None
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str | None = None

    openai_api_key: str | None = None
    openai_model: str | None = None

    anthropic_api_key: str | None = None
    anthropic_model: str | None = None

    openai_compatible_api_key: str | None = None
    openai_compatible_base_url: str | None = None
    openai_compatible_model: str | None = None

    # Encrypts owner-entered provider credentials stored in the
    # `provider_credential` table (see services/secret_store/) so an owner
    # can set/replace/remove API keys from the Settings UI instead of
    # editing this .env file directly. Generate with:
    #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    # Required only to ADD a credential through the UI -- env-var-configured
    # providers keep working with no master key set at all.
    secret_store_master_key: str | None = None

    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    otel_exporter_otlp_endpoint: str | None = None

    # Contact address SEC EDGAR requires in the User-Agent of every request.
    # See https://www.sec.gov/os/webmaster-faq#developers
    sec_edgar_user_agent: str = "earnings-decision-lab research-project@example.com"

    # Pre-live hardening (2026-08-25) -- Operations Monitor's "missed
    # official processing" critical alert (services/operations.py::
    # detect_missed_job_alerts, category="unprocessed_due_event") only
    # fires for a real due event whose next_action_at falls at or after
    # this timestamp. Observability only: never read by any trading-side
    # code, never used to backfill, skip, or otherwise alter a real
    # decision/entry/settlement. None (the default) means no boundary --
    # every real gap is reported, matching this app's behavior before
    # this field existed. Set once, here, when live forward testing
    # actually begins, so real events from before that point (e.g. this
    # project's own pre-activation history) don't permanently read as
    # current production failures.
    forward_test_activation_at: datetime | None = None

    # Phase 4 market-data-quality hardening (2026-08-26), Section 16 --
    # an explicit, deliberately-selected policy rather than a silent
    # convenience default; see MarketDataQualityPolicy's own docstring
    # for why ALLOW_DELAYED_WITH_LABEL preserves this project's actual,
    # already-real capture behavior rather than changing it.
    market_data_quality_policy: MarketDataQualityPolicy = (
        MarketDataQualityPolicy.ALLOW_DELAYED_WITH_LABEL
    )

    # V4.1 methodology foundation (2026-08-31) -- version isolation. V4
    # (analytics/decision/v4_*.py) is experimental and currently inert
    # regardless of these flags (no code path reads them yet to decide
    # which engine actually runs); they exist now so a future task that
    # DOES wire V4 into the real pipeline has an explicit, auditable
    # on/off switch to flip, rather than needing to invent one under
    # time pressure. official_engine_version names which engine's
    # DecisionSnapshot.engine_version the scheduler is authorized to
    # write for real; experimental_engine_v4_enabled gates whether V4
    # may ever run at all. Both default to the real, current, sole state
    # of this project: V3 official, V4 disabled.
    official_engine_version: str = "options-decision-engine-v3"
    experimental_engine_v4_enabled: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
