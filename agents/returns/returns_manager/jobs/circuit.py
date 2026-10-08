"""Circuit breaker pattern per model (§10.4).

Threshold consecutive retryable failures open the circuit for a cooldown duration.
During cooldown, jobs stay pending (fail-open). After cooldown, one half-open probe
is permitted; success resets to CLOSED, failure re-opens.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

logger = logging.getLogger(__name__)


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreaker:
    model_id: str
    failure_threshold: int = 5
    cooldown_s: float = 60.0
    state: CircuitState = CircuitState.CLOSED
    consecutive_failures: int = 0
    opened_at: datetime | None = None
    half_open_probe_active: bool = False
    on_state_change: Callable[[str, CircuitState, CircuitState], None] | None = None

    def allow_request(self, now: datetime | None = None) -> bool:
        """Check whether a request to this model is permitted by circuit state."""
        current_time = now or datetime.now(UTC)

        if self.state == CircuitState.CLOSED:
            return True

        if self.state == CircuitState.OPEN:
            assert self.opened_at is not None
            if current_time >= self.opened_at + timedelta(seconds=self.cooldown_s):
                prev = self.state
                self.state = CircuitState.HALF_OPEN
                self.half_open_probe_active = True
                logger.info(
                    "Circuit breaker for '%s' transitioned from %s to %s", self.model_id, prev, self.state
                )
                if self.on_state_change:
                    self.on_state_change(self.model_id, prev, self.state)
                return True
            return False

        if self.state == CircuitState.HALF_OPEN:
            if not self.half_open_probe_active:
                self.half_open_probe_active = True
                return True
            return False

        return False

    def record_success(self) -> None:
        """Record a successful response; closes the circuit if open or half-open."""
        prev = self.state
        self.consecutive_failures = 0
        self.opened_at = None
        self.half_open_probe_active = False

        if prev != CircuitState.CLOSED:
            self.state = CircuitState.CLOSED
            logger.info("Circuit breaker for '%s' closed after successful probe", self.model_id)
            if self.on_state_change:
                self.on_state_change(self.model_id, prev, self.state)

    def record_failure(self, counts_toward_circuit: bool = True, now: datetime | None = None) -> None:
        """Record a failure against the circuit breaker."""
        if not counts_toward_circuit:
            return

        current_time = now or datetime.now(UTC)
        prev = self.state

        if self.state == CircuitState.HALF_OPEN:
            # Probe failed, reopen
            self.state = CircuitState.OPEN
            self.opened_at = current_time
            self.half_open_probe_active = False
            logger.warning("Circuit breaker probe for '%s' failed; re-opening circuit", self.model_id)
            if self.on_state_change:
                self.on_state_change(self.model_id, prev, self.state)
            return

        if self.state == CircuitState.CLOSED:
            self.consecutive_failures += 1
            if self.consecutive_failures >= self.failure_threshold:
                self.state = CircuitState.OPEN
                self.opened_at = current_time
                logger.warning(
                    "Circuit breaker for '%s' OPENED after %d consecutive failures",
                    self.model_id,
                    self.consecutive_failures,
                )
                if self.on_state_change:
                    self.on_state_change(self.model_id, prev, self.state)


class CircuitBreakerRegistry:
    """Registry maintaining circuit breakers per model."""

    def __init__(self, failure_threshold: int = 5, cooldown_s: float = 60.0) -> None:
        self.default_failure_threshold = failure_threshold
        self.default_cooldown_s = cooldown_s
        self._breakers: dict[str, CircuitBreaker] = {}

    def get(
        self,
        model_id: str,
        failure_threshold: int | None = None,
        cooldown_s: float | None = None,
    ) -> CircuitBreaker:
        if model_id not in self._breakers:
            self._breakers[model_id] = CircuitBreaker(
                model_id=model_id,
                failure_threshold=failure_threshold or self.default_failure_threshold,
                cooldown_s=cooldown_s or self.default_cooldown_s,
            )
        return self._breakers[model_id]

    def reset_all(self) -> None:
        self._breakers.clear()
