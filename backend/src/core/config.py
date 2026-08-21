from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

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

    database_url: str = (
        "postgresql+psycopg://postgres:change_me@localhost:5433/earnings_decision_lab"
    )

    tiingo_api_key: str | None = None
    alpha_vantage_api_key: str | None = None
    options_data_api_key: str | None = None

    # Phase 4 -- the single source of truth for the forward-looking, cross-
    # symbol earnings calendar (see providers/finnhub.py). Deliberately
    # separate from alpha_vantage_api_key: the existing per-ticker "next
    # earnings date" flow (services/market_expectations.py) is untouched
    # and keeps using Alpha Vantage.
    finnhub_api_key: str | None = None

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
