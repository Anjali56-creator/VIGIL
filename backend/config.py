"""Runtime configuration. Secrets come from the environment / .env only - never hard-coded."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- live investigation providers (optional) ---------------------------------
    # The agent layer picks a provider by precedence: Gemini if GEMINI_API_KEY is
    # set, else Anthropic if ANTHROPIC_API_KEY is set, else the deterministic
    # engine-only fallback. No key configured -> fallback. This is the ONLY place
    # provider selection is decided.
    gemini_api_key: str | None = None
    # The "flash-lite" tier is what makes a live investigation demo-fast: a single
    # structured call returns in ~3-6s on the free tier, versus 10-50s for the
    # full "flash" tier. Override with GEMINI_MODEL. The investigation is a single
    # non-agentic call (evidence is pre-gathered), so no tool-calling support is
    # required from the model.
    gemini_model: str = "gemini-flash-lite-latest"

    anthropic_api_key: str | None = None
    vigil_model: str = "claude-opus-5"

    database_url: str = "sqlite:///./vigil.db"

    # Score at or above which a case is opened automatically on ingest.
    case_open_threshold: int = 60

    cors_origins: list[str] = ["*"]

    @staticmethod
    def _set(v: str | None) -> bool:
        return bool(v and v.strip())

    @property
    def active_provider(self) -> str | None:
        """'gemini' | 'anthropic' | None (None => engine-only fallback)."""
        if self._set(self.gemini_api_key):
            return "gemini"
        if self._set(self.anthropic_api_key):
            return "anthropic"
        return None

    @property
    def active_model(self) -> str:
        """Bare model id for the active provider (empty string if none)."""
        return {"gemini": self.gemini_model, "anthropic": self.vigil_model}.get(
            self.active_provider, ""
        )

    @property
    def active_model_label(self) -> str:
        """Human-facing label shown in the UI / health / run records.

        Never says 'Claude' or 'Gemini' unless that provider is actually active.
        """
        p = self.active_provider
        if p == "gemini":
            return f"Gemini {self.gemini_model}"
        if p == "anthropic":
            return f"Claude {self.vigil_model}"
        return "engine-only fallback"

    @property
    def llm_configured(self) -> bool:
        return self.active_provider is not None


settings = Settings()
