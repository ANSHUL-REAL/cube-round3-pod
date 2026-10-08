"""Free-tier request-quota guard (§10.4a). Every model request takes a token from here first; never bypassed.

- `reserve(model, role, n)` atomically reserves n requests of today's budget (Pacific day) in
  `rm.model_request_ledger`, or refuses with QuotaExhaustedError before any request is sent: a session never
  starts that cannot finish. Unused requests are released when the session ends.
- `Reservation.take()` is called once per real request and also waits on a per-model RPM token bucket.
- A provider 429 "daily quota" marks the model exhausted for the rest of the Pacific day.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Literal

from returns_manager.config import Settings
from returns_manager.db.pool import Database
from returns_manager.errors import QuotaExhaustedError
from returns_manager.jobs.budget import (
    BudgetStatus,
    TokenBucketRateLimiter,
    check_and_reserve_budget,
    get_budget_status,
    mark_model_exhausted_for_day,
    release_budget,
)

Role = Literal["judgment", "escalation", "audit", "explainer"]

_LIMITERS: dict[str, TokenBucketRateLimiter] = {}


def daily_budget(settings: Settings, role: Role) -> int:
    if role in ("judgment", "escalation"):  # escalation spends the judgment model's budget (§11.12)
        return settings.rm_daily_request_budget_judgment
    if role == "audit":
        return settings.rm_daily_request_budget_audit
    return settings.rm_daily_request_budget_explainer


def _limiter(model_id: str, rpm: int) -> TokenBucketRateLimiter:
    if model_id not in _LIMITERS:
        _LIMITERS[model_id] = TokenBucketRateLimiter(rpm)
    return _LIMITERS[model_id]


@dataclass
class Reservation:
    model_id: str
    reserved: int
    rpm: int
    used: int = 0
    _limiter: TokenBucketRateLimiter | None = field(default=None, repr=False)

    async def take(self) -> None:
        """Consume one reserved request (and one RPM token). Refuses beyond the reservation."""
        if self.used >= self.reserved:
            raise QuotaExhaustedError(
                f"session budget of {self.reserved} request(s) for {self.model_id} is used up"
            )
        limiter = self._limiter or _limiter(self.model_id, self.rpm)
        if not await limiter.acquire(timeout_s=120):
            raise QuotaExhaustedError(f"RPM limiter for {self.model_id} did not free a slot within 120 s")
        self.used += 1


class QuotaGuard:
    def __init__(self, db: Database, settings: Settings) -> None:
        self.db = db
        self.settings = settings

    async def status(self, model_id: str, role: Role) -> BudgetStatus:
        async with self.db.transaction(None) as conn:
            return await get_budget_status(
                conn, model_id, daily_budget(self.settings, role), self.settings.rm_quota_reset_tz
            )

    @asynccontextmanager
    async def reserve(self, model_id: str, role: Role, n: int) -> AsyncIterator[Reservation]:
        budget = daily_budget(self.settings, role)
        async with self.db.transaction(None) as conn:
            status = await check_and_reserve_budget(
                conn, model_id, budget, count=n, tz_name=self.settings.rm_quota_reset_tz
            )
        if not status.allowed:
            raise QuotaExhaustedError(
                f"daily request budget for {model_id} cannot cover {n} request(s) "
                f"({status.requests_used}/{budget} used; resets {status.next_reset_utc.isoformat()})"
            )
        reservation = Reservation(model_id=model_id, reserved=n, rpm=self.settings.rm_rpm_limit_judgment)
        try:
            yield reservation
        finally:
            unused = reservation.reserved - reservation.used
            if unused > 0:
                async with self.db.transaction(None) as conn:
                    await release_budget(conn, model_id, unused, self.settings.rm_quota_reset_tz)

    async def mark_exhausted(self, model_id: str, role: Role) -> None:
        async with self.db.transaction(None) as conn:
            await mark_model_exhausted_for_day(
                conn, model_id, daily_budget(self.settings, role), self.settings.rm_quota_reset_tz
            )
