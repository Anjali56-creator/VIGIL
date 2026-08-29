"""Runtime configuration. Secrets come from the environment / .env only - never hard-coded."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # None when no key is configured -> the agent layer falls back to a deterministic investigation.
    anthropic_api_key: str | None = None
    database_url: str = "sqlite:///./vigil.db"
    vigil_model: str = "claude-opus-5"

    # Score at or above which a case is opened automatically on ingest.
    case_open_threshold: int = 60

    cors_origins: list[str] = ["*"]

    @property
    def llm_configured(self) -> bool:
        return bool(self.anthropic_api_key and self.anthropic_api_key.strip())


settings = Settings()
