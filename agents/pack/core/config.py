from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    gemini_api_key: str | None = None
    gemini_model: str = "gemini-3.5-flash-lite"
    gemini_timeout_s: float = 25.0
    gemini_max_retries: int = 1
    gemini_thinking_budget: int | None = None

    cost_per_1m_input_usd: float | None = None
    cost_per_1m_output_usd: float | None = None

    database_url: str | None = None
    database_admin_url: str | None = None
    pack_app_db_password: str = "pack_app_dev"
    session_secret: str = "change-me"
    # Send the session cookie only over HTTPS. Turn on wherever the app is served over HTTPS.
    secure_cookies: bool = False

    cache_dir: str = ".cache/vlm"
    catalogue_dir: str = "catalogue"

    # Candidate products shown to the model per box.
    max_candidates: int = 8
    decoys_per_box: int = 2
    ref_images_per_sku: int = 2

    # Decision thresholds. Calibrated on the dev split only, never on the held-out set.
    match_threshold: float = 0.70
    visibility_threshold: float = 0.70

    # Photo quality gate.
    min_side_px: int = 480
    blur_min_var: float = 60.0
    luma_min: float = 45.0
    luma_max: float = 215.0
    max_clipped_pct: float = 8.0
    max_dark_pct: float = 40.0
    max_box_photos: int = 3
    # Web app: at most this many agent checks per organisation per UTC day (0 = no limit).
    # Protects a shared or free-tier API key; boxes over the limit fail open to pending.
    daily_checks_per_org: int = 60
    send_max_side_px: int = 1600
    ref_max_side_px: int = 512

    @field_validator(
        "gemini_thinking_budget", "cost_per_1m_input_usd", "cost_per_1m_output_usd", mode="before"
    )
    @classmethod
    def _blank_is_none(cls, v):
        return None if v == "" else v


@lru_cache
def get_settings() -> Settings:
    return Settings()
