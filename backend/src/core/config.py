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
    earnings_calendar_api_key: str | None = None

    # --- Options-chain provider (provider-agnostic — see providers/base.py
    # and providers/factory.py) ---
    options_provider: str = "alpha_vantage"

    # IBKR Client Portal Gateway (Phase 13). Runs locally on the user's own
    # machine (see docs/ibkr_integration.md) -- this project never talks to
    # IBKR's cloud directly, only to a Gateway instance already
    # authenticated by the user themselves outside this codebase. Read-only:
    # no order-execution endpoint is ever called against this URL.
    ibkr_base_url: str = "https://localhost:5001/v1/api"

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

    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    otel_exporter_otlp_endpoint: str | None = None

    # Contact address SEC EDGAR requires in the User-Agent of every request.
    # See https://www.sec.gov/os/webmaster-faq#developers
    sec_edgar_user_agent: str = "earnings-decision-lab research-project@example.com"


@lru_cache
def get_settings() -> Settings:
    return Settings()
