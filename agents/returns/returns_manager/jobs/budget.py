"""Daily request budget ledger and rate limiting (§10.4a).

Tracks requests per model against daily free-tier quotas reset at Pacific midnight,
and enforces client-side RPM rate limits using a token bucket.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)


def get_pacific_quota_day(tz_name: str = "America/Los_Angeles", now_utc: datetime | None = None) -> date:
    """Get the current calendar date in the quota reset timezone (Pacific)."""
    dt = now_utc or datetime.now(UTC)
    tz = ZoneInfo(tz_name)
    return dt.astimezone(tz).date()


def get_next_pacific_reset(tz_name: str = "America/Los_Angeles", now_utc: datetime | None = None) -> datetime:
    """Calculate the next midnight in the quota reset timezone (Pacific) converted to UTC."""
    dt = now_utc or datetime.now(UTC)
    tz = ZoneInfo(tz_name)
    local_dt = dt.astimezone(tz)
    tomorrow = local_dt.date() + timedelta(days=1)
    next_midnight_local = datetime.combine(tomorrow, datetime.min.time(), tzinfo=tz)
    return next_midnight_local.astimezone(UTC)


@dataclass(frozen=True)
class BudgetStatus:
    model_id: str
    quota_day: date
    requests_used: int
    daily_budget: int
    requests_remaining: int
    allowed: bool
    next_reset_utc: datetime


class TokenBucketRateLimiter:
    """Client-side token bucket rate limiter for RPM control."""

    def __init__(self, rpm: float) -> None:
        self.rpm = max(1.0, rpm)
        self.capacity = self.rpm
        self.tokens = self.capacity
        self.last_update = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: float = 1.0, timeout_s: float = 30.0) -> bool:
        """Attempt to acquire tokens, waiting up to timeout_s if necessary."""
        start_time = time.monotonic()
        fill_rate_per_sec = self.rpm / 60.0

        while True:
            async with self._lock:
                now = time.monotonic()
                elapsed = now - self.last_update
                self.last_update = now
                self.tokens = min(self.capacity, self.tokens + elapsed * fill_rate_per_sec)

                if self.tokens >= tokens:
                    self.tokens -= tokens
                    return True

                needed = tokens - self.tokens
                wait_time = needed / fill_rate_per_sec

            if time.monotonic() - start_time + wait_time > timeout_s:
                return False

            await asyncio.sleep(min(wait_time, 1.0))


async def check_and_reserve_budget(
    conn: Any,
    model_id: str,
    daily_budget: int,
    count: int = 1,
    tz_name: str = "America/Los_Angeles",
) -> BudgetStatus:
    """Atomically check and reserve requests in the model request ledger.

    If usage + count <= daily_budget, increments requests_used and returns allowed=True.
    Otherwise does not increment and returns allowed=False.
    """
    quota_day = get_pacific_quota_day(tz_name)
    next_reset = get_next_pacific_reset(tz_name)

    # Upsert row for today if missing, then lock and check
    res = await conn.execute(
        """
        INSERT INTO rm.model_request_ledger (model_id, quota_day, requests_used, updated_at)
        VALUES (%s, %s, 0, now())
        ON CONFLICT (model_id, quota_day) DO UPDATE SET updated_at = now()
        RETURNING requests_used
        """,
        (model_id, quota_day),
    )
    row = await res.fetchone()
    current_used = row["requests_used"] if row else 0

    if current_used + count <= daily_budget:
        res_upd = await conn.execute(
            """
            UPDATE rm.model_request_ledger
            SET requests_used = requests_used + %s, updated_at = now()
            WHERE model_id = %s AND quota_day = %s
            RETURNING requests_used
            """,
            (count, model_id, quota_day),
        )
        upd_row = await res_upd.fetchone()
        new_used = upd_row["requests_used"] if upd_row else current_used + count
        remaining = max(0, daily_budget - new_used)
        return BudgetStatus(
            model_id=model_id,
            quota_day=quota_day,
            requests_used=new_used,
            daily_budget=daily_budget,
            requests_remaining=remaining,
            allowed=True,
            next_reset_utc=next_reset,
        )
    else:
        remaining = max(0, daily_budget - current_used)
        return BudgetStatus(
            model_id=model_id,
            quota_day=quota_day,
            requests_used=current_used,
            daily_budget=daily_budget,
            requests_remaining=remaining,
            allowed=False,
            next_reset_utc=next_reset,
        )


async def release_budget(
    conn: Any,
    model_id: str,
    count: int,
    tz_name: str = "America/Los_Angeles",
) -> None:
    """Release unused reserved requests back to the ledger (§10.4a)."""
    if count <= 0:
        return
    quota_day = get_pacific_quota_day(tz_name)
    await conn.execute(
        """
        UPDATE rm.model_request_ledger
        SET requests_used = GREATEST(0, requests_used - %s), updated_at = now()
        WHERE model_id = %s AND quota_day = %s
        """,
        (count, model_id, quota_day),
    )


async def mark_model_exhausted_for_day(
    conn: Any,
    model_id: str,
    daily_budget: int,
    tz_name: str = "America/Los_Angeles",
) -> None:
    """Mark a model fully exhausted for the rest of today following a 429 response (§10.4a)."""
    quota_day = get_pacific_quota_day(tz_name)
    await conn.execute(
        """
        INSERT INTO rm.model_request_ledger (model_id, quota_day, requests_used, updated_at)
        VALUES (%s, %s, %s, now())
        ON CONFLICT (model_id, quota_day) DO UPDATE
        SET requests_used = GREATEST(rm.model_request_ledger.requests_used, EXCLUDED.requests_used),
            updated_at = now()
        """,
        (model_id, quota_day, daily_budget),
    )


async def get_budget_status(
    conn: Any,
    model_id: str,
    daily_budget: int,
    tz_name: str = "America/Los_Angeles",
) -> BudgetStatus:
    """Read current budget status for a model without modifying usage."""
    quota_day = get_pacific_quota_day(tz_name)
    next_reset = get_next_pacific_reset(tz_name)

    res = await conn.execute(
        """
        SELECT requests_used FROM rm.model_request_ledger
        WHERE model_id = %s AND quota_day = %s
        """,
        (model_id, quota_day),
    )
    row = await res.fetchone()
    used = row["requests_used"] if row else 0
    remaining = max(0, daily_budget - used)

    return BudgetStatus(
        model_id=model_id,
        quota_day=quota_day,
        requests_used=used,
        daily_budget=daily_budget,
        requests_remaining=remaining,
        allowed=remaining > 0,
        next_reset_utc=next_reset,
    )
