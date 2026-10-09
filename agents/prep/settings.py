"""Prep Manager settings: the vision model and its time and cost limits.

Keys come from the environment (or the pod's git-ignored ``.env``), never from code. Prices are not guessed:
with no ``COST_PER_1M_*`` set, ``model.cost_usd`` is ``null`` and the token counts are still recorded.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    gemini_api_key: str | None = None
    prep_gemini_model: str = "gemini-3.5-flash-lite"
    # The orchestrator gives a stage 75 s (D-O07). 2 attempts x 28 s + the 2 s back-off = 58 s worst case, so a slow model
    # becomes a retryable *pending* record instead of an orchestrator timeout that loses the capture.
    prep_timeout_s: float = 28.0
    prep_max_retries: int = 1
    prep_max_photos: int = 6
    prep_send_max_side_px: int = 1600

    cost_per_1m_input_usd: float | None = None
    cost_per_1m_output_usd: float | None = None

    @field_validator("gemini_api_key", "cost_per_1m_input_usd", "cost_per_1m_output_usd", mode="before")
    @classmethod
    def _blank_is_none(cls, v):
        return None if v == "" else v


@lru_cache
def get_settings() -> Settings:
    return Settings()
