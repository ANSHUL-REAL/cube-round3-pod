"""Spend guards for bulk model work (§18.4): eval runs and live load tests.

Before either can start, `preflight_bulk_spend` computes how many model requests the run
would need, checks that against what remains of today's daily quota (§10.4a), and reports
how many Pacific-midnight resets the run would need to finish. It also checks the
paid-equivalent cost estimate against a spend cap. Both checks can be overridden — quota
with `allow_multi_day`, cost with `confirm_spend` — but never silently: a caller must pass
the flag, and the refusal always names the exact numbers that were checked, so the human
approving `--confirm-spend` sees what they are approving.

This module never sends a request itself; it is a pure precondition on top of
`llm/quota.py` (`BudgetStatus`) and `llm/pricing.py` (`cost_usd_micros`). P12 (eval) and
P13 (load-test) call it; it is exercised standalone here because nothing about it depends
on either phase existing yet.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal

from returns_manager.errors import SpendGuardRefused
from returns_manager.jobs.budget import BudgetStatus


@dataclass(frozen=True)
class SpendPreflight:
    """Recorded verbatim in the run manifest (§18.4). Only ever constructed on success —
    `preflight_bulk_spend` raises `SpendGuardRefused` instead of returning a refused one."""

    model_id: str
    units: int
    expected_requests_per_unit: float
    requests_needed: int
    requests_remaining_today: int
    daily_budget: int
    days_needed: int
    estimated_cost_usd: Decimal
    spend_cap_usd: Decimal

    def to_dict(self) -> dict[str, object]:
        return {
            "model_id": self.model_id,
            "units": self.units,
            "expected_requests_per_unit": self.expected_requests_per_unit,
            "requests_needed": self.requests_needed,
            "requests_remaining_today": self.requests_remaining_today,
            "daily_budget": self.daily_budget,
            "days_needed": self.days_needed,
            "estimated_cost_usd": str(self.estimated_cost_usd),
            "spend_cap_usd": str(self.spend_cap_usd),
        }


def _days_needed(requests_needed: int, remaining_today: int, daily_budget: int) -> int:
    if requests_needed <= remaining_today:
        return 1 if requests_needed > 0 else 0
    still_needed = requests_needed - remaining_today
    return 1 + math.ceil(still_needed / max(daily_budget, 1))


def preflight_bulk_spend(
    *,
    model_id: str,
    units: int,
    expected_requests_per_unit: float,
    estimated_cost_usd_per_unit: Decimal,
    budget_status: BudgetStatus,
    spend_cap_usd: Decimal,
    allow_multi_day: bool = False,
    confirm_spend: bool = False,
) -> SpendPreflight:
    """Compute the preflight numbers and enforce both guards.

    Raises `SpendGuardRefused` (exit code 4) when either guard trips without its override —
    this function enforces, it never returns a "not allowed" result silently. On success it
    returns the full `SpendPreflight` (`allowed=True`) to record in the run manifest.
    """
    requests_needed = math.ceil(units * expected_requests_per_unit)
    days_needed = _days_needed(requests_needed, budget_status.requests_remaining, budget_status.daily_budget)
    estimated_cost_usd = (estimated_cost_usd_per_unit * units).quantize(Decimal("0.0001"))

    if days_needed > 1 and not allow_multi_day:
        raise SpendGuardRefused(
            f"{model_id}: this run needs {requests_needed} requests but only "
            f"{budget_status.requests_remaining}/{budget_status.daily_budget} remain today "
            f"(resets {budget_status.next_reset_utc.isoformat()}); it would take {days_needed} "
            "Pacific-day resets to finish. Re-run with --allow-multi-day to proceed across "
            "multiple days, or reduce the unit count."
        )

    if spend_cap_usd > 0 and estimated_cost_usd > spend_cap_usd and not confirm_spend:
        raise SpendGuardRefused(
            f"{model_id}: estimated paid-equivalent cost ${estimated_cost_usd} for {units} unit(s) "
            f"exceeds the ${spend_cap_usd} cap. Re-run with --confirm-spend to proceed anyway."
        )

    return SpendPreflight(
        model_id=model_id,
        units=units,
        expected_requests_per_unit=expected_requests_per_unit,
        requests_needed=requests_needed,
        requests_remaining_today=budget_status.requests_remaining,
        daily_budget=budget_status.daily_budget,
        days_needed=days_needed,
        estimated_cost_usd=estimated_cost_usd,
        spend_cap_usd=spend_cap_usd,
    )
