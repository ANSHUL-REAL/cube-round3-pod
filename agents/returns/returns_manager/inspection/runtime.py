"""Wiring the judgment handler for the worker and the CLI: model client, photo source, quota guard.

`RM_MODEL_PROVIDER=gemini` needs GEMINI_API_KEY and Supabase storage (photos are read from the private
bucket).
`replay` is for tests only (cassettes are passed explicitly there), so it is refused here.
"""

from __future__ import annotations

from dataclasses import dataclass

from returns_manager.config import Settings
from returns_manager.db.pool import Database
from returns_manager.errors import ConfigError
from returns_manager.inspection.service import JudgmentHandler
from returns_manager.llm.gemini_client import GeminiModelClient
from returns_manager.llm.quota import QuotaGuard
from returns_manager.storage.photos import PhotoStorage


@dataclass(frozen=True)
class Runtime:
    handler: JudgmentHandler
    client: GeminiModelClient
    storage: PhotoStorage
    quota: QuotaGuard


def build_runtime(db: Database, settings: Settings, eval_run_id: str | None = None) -> Runtime:
    if settings.rm_model_provider != "gemini":
        raise ConfigError("RM_MODEL_PROVIDER=replay is for tests (cassettes are passed explicitly)")
    settings.require("gemini_api_key", "supabase_url")
    assert settings.gemini_api_key is not None
    client = GeminiModelClient(settings.gemini_api_key.get_secret_value(), settings.rm_model_timeout_s)
    storage = PhotoStorage(settings)
    quota = QuotaGuard(db, settings)
    return Runtime(
        JudgmentHandler(db, settings, client, storage, quota, eval_run_id=eval_run_id),
        client,
        storage,
        quota,
    )
