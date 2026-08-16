from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"
    log_level: str = "INFO"

    database_url: str = (
        "postgresql+psycopg://postgres:change_me@localhost:5433/earnings_decision_lab"
    )

    market_data_api_key: str | None = None
    options_data_api_key: str | None = None
    earnings_calendar_api_key: str | None = None

    anthropic_api_key: str | None = None
    llm_model: str | None = None

    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    otel_exporter_otlp_endpoint: str | None = None

    # Contact address SEC EDGAR requires in the User-Agent of every request.
    # See https://www.sec.gov/os/webmaster-faq#developers
    sec_edgar_user_agent: str = "earnings-decision-lab research-project@example.com"


@lru_cache
def get_settings() -> Settings:
    return Settings()
