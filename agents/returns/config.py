"""Settings for the Returns agent. Environment variables (or the pod's .env, which is git-ignored) only; no secrets here."""
from __future__ import annotations

from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    gemini_api_key: str | None = None
    # The Round 2 default for the judgment model (RM_JUDGMENT_MODEL). Not verified against any key from this repo.
    returns_model: str = "gemini-3.8-flash"
    # Each attempt is bounded by this; with the one retry below the stage still ends inside flow.json's timeout.
    returns_model_timeout_s: float = 28.0
    # One retry on a busy (429/5xx) or timed-out call; the retry uses the fallback model when it is set.
    # 2 x 28 s + 2 s back-off = 58 s, inside the stage timeout in flow.json (75 s, decision D-O07).
    returns_model_retries: int = 1
    returns_fallback_model: str = "gemini-3.5-flash-lite"
    returns_retry_backoff_s: float = 2.0
    # Round 2 used "medium". "low" is the default here only to fit the stage timeout; neither is measured through the pod.
    returns_thinking: Literal["minimal", "low", "medium", "high"] = "low"
    # json_prompted: the schema is in the task text (works on any endpoint). json_schema: also asks for constrained output.
    # json_schema: Gemini is held to the judgment schema. With json_prompted, gemini-3.8-flash added fields the strict
    # schema forbids and every answer was thrown away (live rehearsal, 2026-10-09); json_schema validated on both models.
    returns_output_mode: Literal["json_schema", "json_prompted"] = "json_schema"
    returns_max_output_tokens: int = 16000
    # Photos per return the model is shown (Round 2: 1 to 3). Extra captures are listed, not silently dropped.
    returns_max_photos: int = 3
    cost_per_1m_input_usd: float | None = None
    cost_per_1m_output_usd: float | None = None

    @field_validator("cost_per_1m_input_usd", "cost_per_1m_output_usd", "gemini_api_key", mode="before")
    @classmethod
    def _blank_is_none(cls, v):
        return None if v == "" else v
