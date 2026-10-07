"""Settings for the Receiving Manager. Read from the environment (or the pod's git-ignored .env), never from code."""
from __future__ import annotations

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    gemini_api_key: str | None = None
    # Same variable names as the Pack Manager, so one .env serves both. This default has NOT been evaluated for
    # receiving: Round 2 never ran the live model on photos (see README, Limits).
    gemini_model: str = "gemini-3.5-flash-lite"
    cost_per_1m_input_usd: float | None = None
    cost_per_1m_output_usd: float | None = None

    # Rules thresholds. Defaults are conservative guesses, not tuned on any data: they only ever turn a verdict into
    # UNCERTAIN (never into PASS or FAIL), so a wrong value costs a human look, not a wrong claim.
    rcv_min_clarity: float = 0.5        # image_clarity below this: observation-based checks are UNCERTAIN (poor_image)
    rcv_min_confidence: float = 0.6     # a group's self-reported confidence below this: UNCERTAIN (insufficient_evidence)

    max_photos: int = 6                 # photos sent in the one batched call; extras are listed, not silently dropped
    send_max_side_px: int = 1600        # photos are downscaled to this before upload (the original hash is recorded)

    @field_validator("gemini_api_key", "cost_per_1m_input_usd", "cost_per_1m_output_usd", mode="before")
    @classmethod
    def _blank_is_none(cls, v):
        return None if v == "" else v
