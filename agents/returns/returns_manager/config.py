"""Settings (build prompt §5).

Every variable is validated when `get_settings()` is first called. Values that only some commands need
(database URLs, Supabase keys, the Gemini key) are optional at load time; a command declares what it
needs with `settings.require(...)`, which fails with a clear message and exit code 5. Secret values are
`SecretStr` and never appear in messages, logs or `repr`.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from returns_manager.errors import ConfigError

# agent/src/returns_manager/config.py -> parents[3] is the repository root.
REPO_ROOT = Path(__file__).resolve().parents[3]
AGENT_ROOT = REPO_ROOT / "agent"

Thinking = Literal["low", "medium", "high"]
ImageResolution = Literal["low", "medium", "high", "ultra_high"]


def _env_files() -> tuple[Path, ...]:
    override = os.environ.get("RM_ENV_FILE")
    if override:
        return (Path(override),)
    return (REPO_ROOT / ".env",)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_env_files(),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        hide_input_in_errors=True,
    )

    # ── Gemini (free tier) ────────────────────────────────────────────
    gemini_api_key: SecretStr | None = None
    rm_model_provider: Literal["gemini", "replay"] = "gemini"
    rm_judgment_model: str = "gemini-3.8-flash"
    rm_judgment_thinking: Thinking = "medium"
    rm_judgment_fallback_model: str = "gemini-3-flash-preview"
    rm_target_p95_latency_s: int = Field(45, gt=0)
    rm_target_mean_cost_usd: str = "0.01"  # decimal string; paid-equivalent
    rm_escalation_model: str = "gemini-3.8-flash"
    rm_escalation_thinking: Thinking = "high"
    rm_audit_model: str = "gemini-3.6-flash"
    rm_audit_thinking: Thinking = "medium"
    rm_audit_sample_rate: float = Field(0.0, ge=0.0, le=1.0)
    rm_explainer_model: str = "gemini-3.1-flash-lite"
    rm_explainer_thinking: Thinking = "low"
    rm_daily_request_budget_judgment: int = Field(18, ge=0)
    rm_daily_request_budget_audit: int = Field(18, ge=0)
    rm_daily_request_budget_explainer: int = Field(50, ge=0)
    rm_rpm_limit_judgment: int = Field(8, gt=0)
    rm_quota_reset_tz: str = "America/Los_Angeles"
    rm_output_mode: Literal["json_schema", "json_prompted"] = "json_schema"
    rm_max_output_tokens: int = Field(16000, gt=0, le=65536)
    rm_max_round_trips: int = Field(2, ge=1, le=5)
    rm_max_crops_per_session: int = Field(4, ge=0, le=16)
    rm_model_timeout_s: int = Field(180, gt=0)
    rm_return_photo_resolution: ImageResolution = "high"
    rm_reference_photo_resolution: ImageResolution = "medium"
    rm_crop_resolution: ImageResolution = "ultra_high"
    rm_analysis_long_edge: int = Field(1568, ge=256, le=4096)
    rm_crop_max_edge: int = Field(1568, ge=256, le=4096)
    rm_use_files_api_for_references: bool = True
    rm_interaction_store: bool = True

    # ── Database / Supabase ───────────────────────────────────────────
    database_url: SecretStr | None = None  # role rm_app_login ONLY (non-bypass)
    database_migrator_url: SecretStr | None = None  # owner role; used ONLY by `db migrate`
    supabase_url: str | None = None
    supabase_anon_key: SecretStr | None = None
    supabase_service_role_key: SecretStr | None = None  # storage module ONLY
    supabase_jwks_url: str | None = None
    supabase_jwt_issuer: str | None = None
    supabase_jwt_audience: str = "authenticated"
    supabase_jwt_secret: SecretStr | None = None  # only if the project still signs with legacy HS256
    rm_storage_bucket_photos: str = "rm-return-photos"
    rm_storage_bucket_reference: str = "rm-reference"
    rm_signed_url_ttl_s: int = Field(300, gt=0, le=3600)

    # ── Behaviour ─────────────────────────────────────────────────────
    rm_env: Literal["local", "test", "demo"] = "local"
    rm_target_marketplace: str = "amazon.in"
    rm_worker_concurrency: int = Field(6, ge=1, le=64)
    rm_max_inflight_per_org: int = Field(4, ge=1)
    rm_job_max_attempts: int = Field(5, ge=1)
    rm_job_lease_s: int = Field(600, gt=0)
    rm_circuit_failure_threshold: int = Field(5, ge=1)
    rm_circuit_cooldown_s: int = Field(60, ge=1)
    rm_high_value_threshold_minor: int = Field(500000, ge=0)
    rm_max_daily_spend_usd: str = "0"
    rm_eval_spend_cap_usd: str = "0"
    rm_webhook_allowlist: str = ""
    log_level: Literal["debug", "info", "warning", "error"] = "info"

    @field_validator("rm_target_mean_cost_usd", "rm_max_daily_spend_usd", "rm_eval_spend_cap_usd")
    @classmethod
    def _decimal_string(cls, v: str) -> str:
        from decimal import Decimal, InvalidOperation

        try:
            if Decimal(v) < 0:
                raise ValueError("must be >= 0")
        except InvalidOperation as exc:
            raise ValueError("must be a decimal number written as a string, e.g. '0.01'") from exc
        return v

    def require(self, *fields: str) -> None:
        """Fail with exit code 5 if any named setting is unset or empty (never prints values)."""
        missing = []
        for name in fields:
            value = getattr(self, name)
            if isinstance(value, SecretStr):
                value = value.get_secret_value()
            if value is None or value == "":
                missing.append(name.upper())
        if missing:
            raise ConfigError(
                "Missing required configuration: "
                + ", ".join(missing)
                + ". Set them in the repository-root .env (see .env.example)."
            )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']).upper()}: {err['msg']}" for err in exc.errors()
        )
        raise ConfigError(f"Invalid configuration: {problems}") from None
