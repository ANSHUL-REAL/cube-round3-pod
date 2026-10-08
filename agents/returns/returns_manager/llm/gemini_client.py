"""Gemini Interactions API client (§11.1), google-genai 2.25.0 (signatures read from the installed source).

- Async: `client.aio.interactions.create(**request.body())`.
- **SDK automatic retries are off** (`HttpRetryOptions(attempts=0)`). The SDK would otherwise silently retry
  408/409/429/5xx up to 4 times: a 429 retry burns free-tier daily quota and stretches the job lease. Retries
  belong to the job layer (§10.4). Note: in this SDK version the interactions path uses `attempts` as the
  *retry* count, although the type's docstring says "including the original request" (finding F-011).
- Per-request timeout from `RM_MODEL_TIMEOUT_S`.
- Errors are mapped onto the §10.4 classes by HTTP status and SDK class name (duck typing: the SDK's error
  classes live in a private module).
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from returns_manager.llm.client import ModelRequest, ModelResponse, ProviderError, response_from_raw

_RETRY_DELAY_RE = re.compile(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"')


def build_sdk_client(api_key: str, timeout_s: int) -> Any:
    from google import genai
    from google.genai import types

    client = genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(
            timeout=timeout_s * 1000,  # milliseconds
            retry_options=types.HttpRetryOptions(attempts=1),
        ),
    )
    # google-genai 2.25.0 has no way to express "no retries" for Interactions through HttpRetryOptions: the
    # legacy client rewrites attempts=0 to 1 in place (_api_client.py), and the Interactions path reads
    # `attempts` as the RETRY count, so the best it can give is one silent retry. The generated `create` only
    # retries when its configuration holds a RetryConfig, so it is cleared here (tested; finding F-011).
    for resource in (client.interactions, client.aio.interactions):
        resource.sdk_configuration.retry_config = None
    return client


def classify_provider_exception(exc: BaseException) -> ProviderError:
    """Map an SDK/transport exception onto a ProviderError with a §10.4 class. Never includes secrets."""
    name = type(exc).__name__
    status = getattr(exc, "status_code", None)
    text = str(exc)
    body = getattr(exc, "body", None)
    blob = f"{text} {json.dumps(body) if isinstance(body, dict | list) else body or ''}"
    lowered = blob.lower()
    if "timeout" in name.lower() or isinstance(exc, TimeoutError):
        return ProviderError("timeout", f"model request timed out ({name})")
    if isinstance(status, int):
        if status == 429:
            daily = "perday" in lowered.replace("_", "").replace(" ", "") or "per day" in lowered
            if daily:
                return ProviderError(
                    "quota_exhausted", "429 RESOURCE_EXHAUSTED: daily request quota used up", status_code=429
                )
            m = _RETRY_DELAY_RE.search(blob)
            return ProviderError(
                "rate_limit",
                "429 rate limited (per-minute)",
                status_code=429,
                retry_after_s=float(m.group(1)) if m else 60.0,
            )
        if status in (401, 403):
            return ProviderError("auth", f"{status} authentication/permission error", status_code=status)
        if status == 404:
            return ProviderError("not_found", "404 model or resource not found", status_code=404)
        if status in (400, 422):
            return ProviderError(
                "invalid_request", f"{status} invalid request: {text[:300]}", status_code=status
            )
        if status >= 500 or status == 408:
            return ProviderError("server_error", f"{status} server error / overloaded", status_code=status)
    if "connection" in name.lower() or isinstance(exc, ConnectionError | OSError):
        return ProviderError("network", f"network error ({name})")
    return ProviderError("server_error", f"unexpected provider error ({name}): {text[:300]}")


class GeminiModelClient:
    def __init__(self, api_key: str, timeout_s: int, sdk_client: Any | None = None) -> None:
        self._client = sdk_client or build_sdk_client(api_key, timeout_s)

    async def create(self, request: ModelRequest) -> ModelResponse:
        started = time.perf_counter()
        try:
            interaction = await self._client.aio.interactions.create(**request.body())
        except Exception as exc:
            raise classify_provider_exception(exc) from None
        latency_ms = int((time.perf_counter() - started) * 1000)
        raw = interaction.model_dump(mode="json", by_alias=True, exclude_none=True)
        return response_from_raw(raw, latency_ms)

    async def list_model_ids(self) -> list[str]:
        """Readiness check (§11.1): listing models spends no generation quota."""
        names: list[str] = []
        pager = await self._client.aio.models.list()
        async for m in pager:
            names.append(str(getattr(m, "name", "")).removeprefix("models/"))
        return names
