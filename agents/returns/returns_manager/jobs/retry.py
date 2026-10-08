"""Error classification, retry logic, and exponential backoff (§10.4)."""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from returns_manager.errors import (
    CircuitOpenError,
    ConfigError,
    HandlerNotConfigured,
    InvalidTransitionError,
    NotBuiltYet,
    QuotaExhaustedError,
)
from returns_manager.llm.client import ProviderError


@dataclass(frozen=True)
class ErrorClassification:
    error_class: str
    retryable: bool
    counts_toward_circuit: bool
    action: str  # "retry", "wait", "needs_attention"
    detail: str
    suggested_wait_s: float | None = None
    wait_until: datetime | None = None


def compute_next_attempt_at(
    attempts: int,
    base_delay_s: float = 5.0,
    max_delay_s: float = 300.0,
    jitter_factor: float | None = None,
    now: datetime | None = None,
) -> datetime:
    """Compute the next attempt timestamp using exponential backoff with jitter (§10.4).

    Formula: now + min(300 s, 2^attempts * 5 s) * uniform(0.8, 1.2)
    """
    current_time = now or datetime.now(UTC)
    # Jitter spreads retries; it is not security-relevant (S311). An explicit factor is clamped for safety.
    jitter_factor = (
        random.uniform(0.8, 1.2)  # noqa: S311
        if jitter_factor is None
        else max(0.5, min(1.5, jitter_factor))
    )

    # attempts >= 0; 2^attempts * base_delay
    raw_delay = (2**attempts) * base_delay_s
    clamped_delay = min(max_delay_s, raw_delay)
    final_delay = clamped_delay * jitter_factor

    return current_time + timedelta(seconds=final_delay)


# Provider failures already classified by the model client (llm.client.ProviderError): §10.4 table.
# error_class → (retryable, counts_toward_circuit, action)
_PROVIDER_CLASSES: dict[str, tuple[bool, bool, str]] = {
    "quota_exhausted": (False, False, "wait"),
    "rate_limit": (True, True, "retry"),
    "server_error": (True, True, "retry"),
    "timeout": (True, True, "retry"),
    "network": (True, True, "retry"),
    "truncated": (True, False, "retry"),
    "schema_error": (True, False, "retry"),
    "safety_blocked": (False, False, "needs_attention"),
    "invalid_request": (False, False, "needs_attention"),
    "auth": (False, False, "needs_attention"),
    "not_found": (False, False, "needs_attention"),
}


def _classify_type(exc: Exception | str) -> ErrorClassification | None:
    """Our own exception types and Python's network/timeout errors are classified by type, not by text."""
    if isinstance(exc, ProviderError) and exc.error_class in _PROVIDER_CLASSES:
        retryable, counts, action = _PROVIDER_CLASSES[exc.error_class]
        return ErrorClassification(
            error_class=exc.error_class,
            retryable=retryable,
            counts_toward_circuit=counts,
            action=action,
            detail=str(exc),
            suggested_wait_s=exc.retry_after_s,
        )
    if isinstance(exc, HandlerNotConfigured | ConfigError | NotBuiltYet):
        return ErrorClassification(
            error_class="configuration",
            retryable=False,
            counts_toward_circuit=False,
            action="needs_attention",
            detail=str(exc),
        )
    if isinstance(exc, InvalidTransitionError):
        return ErrorClassification(
            error_class="invalid_state",
            retryable=False,
            counts_toward_circuit=False,
            action="needs_attention",
            detail=str(exc),
        )
    if isinstance(exc, QuotaExhaustedError):
        return ErrorClassification(
            error_class="quota_exhausted",
            retryable=False,
            counts_toward_circuit=False,
            action="wait",
            detail=str(exc),
        )
    if isinstance(exc, CircuitOpenError):
        return ErrorClassification(
            error_class="circuit_open",
            retryable=True,
            counts_toward_circuit=False,
            action="wait",
            detail=str(exc),
        )
    if isinstance(exc, TimeoutError):
        return ErrorClassification(
            error_class="timeout",
            retryable=True,
            counts_toward_circuit=True,
            action="retry",
            detail=f"Timed out: {exc}",
        )
    if isinstance(exc, ConnectionError):
        return ErrorClassification(
            error_class="network",
            retryable=True,
            counts_toward_circuit=True,
            action="retry",
            detail=f"Network error: {exc}",
        )
    return None


def classify_error(
    exc: Exception | str, status_code: int | None = None, extra: dict[str, Any] | None = None
) -> ErrorClassification:
    """Classify an exception or error condition into standardized error classes (§10.4).

    Typed exceptions first; provider errors (whose exact classes are verified in P5) by status code and
    message. Anything unrecognised is retried with backoff but does not count toward the circuit breaker,
    because it is more likely a bug on our side than a provider outage.
    """
    typed = _classify_type(exc)
    if typed is not None:
        return typed
    exc_str = str(exc).lower()
    extra = extra or {}

    # 1. Kill switches and operational holds
    if "model_calls_disabled" in exc_str or extra.get("reason") == "model_calls_disabled":
        return ErrorClassification(
            error_class="model_calls_disabled",
            retryable=False,
            counts_toward_circuit=False,
            action="wait",
            detail="Model calls are disabled via system controls kill switch",
        )

    if "budget_exhausted" in exc_str or extra.get("reason") == "budget_exhausted":
        return ErrorClassification(
            error_class="budget_exhausted",
            retryable=False,
            counts_toward_circuit=False,
            action="wait",
            detail="Daily spend budget reached",
        )

    # 2. Daily quota vs Rate limit (429)
    if status_code == 429 or "resource_exhausted" in exc_str or "quota" in exc_str or "rate limit" in exc_str:
        if "daily" in exc_str or "day" in exc_str or "quota_exhausted" in exc_str:
            wait_until = extra.get("next_reset")
            return ErrorClassification(
                error_class="quota_exhausted",
                retryable=False,
                counts_toward_circuit=False,
                action="wait",
                detail="Daily model request quota exhausted "
                f"(reset at {wait_until or 'next Pacific midnight'})",
                wait_until=wait_until,
            )
        # Per-minute rate limit
        retry_delay = extra.get("retry_after_s", 60.0)
        return ErrorClassification(
            error_class="rate_limit",
            retryable=True,
            counts_toward_circuit=True,
            action="retry",
            suggested_wait_s=retry_delay,
            detail="Per-minute rate limit hit (429 RPM)",
        )

    # 3. Truncation
    if extra.get("finish_reason") == "MAX_TOKENS" or "truncated" in exc_str:
        return ErrorClassification(
            error_class="truncated",
            retryable=True,
            counts_toward_circuit=False,
            action="retry",
            detail="Model output reached max_output_tokens limit",
        )

    # 4. Schema validation errors
    if "schema" in exc_str or "validation" in exc_str or extra.get("is_schema_error"):
        return ErrorClassification(
            error_class="schema_error",
            retryable=True,
            counts_toward_circuit=False,
            action="retry",
            detail="Model output failed schema validation",
        )

    # 5. Safety blocks
    if "safety" in exc_str or extra.get("finish_reason") == "SAFETY":
        return ErrorClassification(
            error_class="safety_blocked",
            retryable=False,
            counts_toward_circuit=False,
            action="needs_attention",
            detail="Content blocked by safety filters",
        )

    # 6. Auth / Client errors
    if status_code in (401, 403) or "unauthorized" in exc_str or "permission" in exc_str:
        return ErrorClassification(
            error_class="auth",
            retryable=False,
            counts_toward_circuit=False,
            action="needs_attention",
            detail=f"Authentication/authorization failure: {exc}",
        )

    if status_code == 404 or "not found" in exc_str:
        return ErrorClassification(
            error_class="not_found",
            retryable=False,
            counts_toward_circuit=False,
            action="needs_attention",
            detail=f"Resource or model not found: {exc}",
        )

    if status_code == 400 or "invalid_argument" in exc_str or "bad request" in exc_str:
        return ErrorClassification(
            error_class="invalid_request",
            retryable=False,
            counts_toward_circuit=False,
            action="needs_attention",
            detail=f"Invalid request parameters: {exc}",
        )

    # 7. Worker shutdown and lease expiration
    if exc_str == "shutdown" or extra.get("reason") == "shutdown":
        return ErrorClassification(
            error_class="shutdown",
            retryable=True,
            counts_toward_circuit=False,
            action="retry",
            detail="Worker initiated graceful shutdown",
        )

    if exc_str == "lease_expired" or extra.get("reason") == "lease_expired":
        return ErrorClassification(
            error_class="lease_expired",
            retryable=True,
            counts_toward_circuit=False,
            action="retry",
            detail="Worker lease expired",
        )

    # 8. Server errors, timeouts, network
    if (
        status_code in (500, 502, 503, 504)
        or "timeout" in exc_str
        or "connection" in exc_str
        or "network" in exc_str
        or "overloaded" in exc_str
    ):
        return ErrorClassification(
            error_class="server_error",
            retryable=True,
            counts_toward_circuit=True,
            action="retry",
            detail=f"Server or network error: {exc}",
        )

    # Unknown: retry with backoff (bounded by max_attempts), but never trip the provider circuit.
    return ErrorClassification(
        error_class="unexpected_error",
        retryable=True,
        counts_toward_circuit=False,
        action="retry",
        detail=f"Unexpected error: {exc}",
    )
