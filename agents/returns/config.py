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
    # One attempt, bounded to finish inside the orchestrator's 30 s stage timeout (flow.json defaults.timeout_s).
    returns_model_timeout_s: float = 24.0
    # Round 2 used "medium". "low" is the default here only to fit the stage timeout; neither is measured through the pod.
    returns_thinking: Literal["minimal", "low", "medium", "high"] = "low"
    # json_prompted: the schema is in the task text (works on any endpoint). json_schema: also asks for constrained output.
    returns_output_mode: Literal["json_schema", "json_prompted"] = "json_prompted"
    returns_max_output_tokens: int = 16000
    # Photos per return the model is shown (Round 2: 1 to 3). Extra captures are listed, not silently dropped.
    returns_max_photos: int = 3
    cost_per_1m_input_usd: float | None = None
    cost_per_1m_output_usd: float | None = None

    @field_validator("cost_per_1m_input_usd", "cost_per_1m_output_usd", "gemini_api_key", mode="before")
    @classmethod
    def _blank_is_none(cls, v):
        return None if v == "" else v
