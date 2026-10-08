"""Data models and report structures for load testing and resilience drills (§19, §20, §23)."""

from __future__ import annotations

import random
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

import numpy as np


class LoadMode(StrEnum):
    REPLAY = "replay"
    LIVE = "live"


@dataclass(frozen=True)
class LatencyProfile:
    """Latency profile controlling simulated model delay in replay mode (§19)."""

    kind: str  # "instant", "fixed", "uniform"
    fixed_ms: float = 0.0
    min_ms: float = 0.0
    max_ms: float = 0.0
    raw: str = "instant"

    def sample_delay_s(self) -> float:
        """Sample a sleep duration in seconds according to this profile."""
        if self.kind == "instant":
            return 0.0
        if self.kind == "fixed":
            return max(0.0, self.fixed_ms / 1000.0)
        if self.kind == "uniform":
            ms = random.uniform(self.min_ms, self.max_ms)  # noqa: S311
            return max(0.0, ms / 1000.0)
        return 0.0


@dataclass
class UnitExecutionRecord:
    """Per-unit timing and outcome record during a load test run."""

    unit_id: str
    return_id: str
    job_id: str
    enqueued_at: datetime
    claimed_at: datetime | None = None
    completed_at: datetime | None = None
    duration_ms: float = 0.0
    queue_wait_ms: float = 0.0
    total_latency_ms: float = 0.0
    status: str = "pending"
    error_class: str | None = None
    error_detail: str | None = None

    def finalize_timings(self) -> None:
        """Calculate durations once completed_at is set."""
        if self.claimed_at and self.enqueued_at:
            self.queue_wait_ms = max(0.0, (self.claimed_at - self.enqueued_at).total_seconds() * 1000.0)
        if self.completed_at and self.claimed_at:
            self.duration_ms = max(0.0, (self.completed_at - self.claimed_at).total_seconds() * 1000.0)
        if self.completed_at and self.enqueued_at:
            self.total_latency_ms = max(0.0, (self.completed_at - self.enqueued_at).total_seconds() * 1000.0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "unit_id": self.unit_id,
            "return_id": self.return_id,
            "job_id": self.job_id,
            "enqueued_at": self.enqueued_at.isoformat(),
            "claimed_at": self.claimed_at.isoformat() if self.claimed_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "duration_ms": round(self.duration_ms, 2),
            "queue_wait_ms": round(self.queue_wait_ms, 2),
            "total_latency_ms": round(self.total_latency_ms, 2),
            "status": self.status,
            "error_class": self.error_class,
            "error_detail": self.error_detail,
        }


@dataclass(frozen=True)
class LatencyPercentiles:
    """Distribution of measured latency (§23: p50/p95/p99)."""

    p50_ms: float
    p90_ms: float
    p95_ms: float
    p99_ms: float
    min_ms: float
    max_ms: float
    mean_ms: float

    @classmethod
    def compute(cls, values: list[float]) -> LatencyPercentiles:
        if not values:
            return cls(
                p50_ms=0.0,
                p90_ms=0.0,
                p95_ms=0.0,
                p99_ms=0.0,
                min_ms=0.0,
                max_ms=0.0,
                mean_ms=0.0,
            )
        arr = np.array(values, dtype=float)
        return cls(
            p50_ms=round(float(np.percentile(arr, 50)), 2),
            p90_ms=round(float(np.percentile(arr, 90)), 2),
            p95_ms=round(float(np.percentile(arr, 95)), 2),
            p99_ms=round(float(np.percentile(arr, 99)), 2),
            min_ms=round(float(np.min(arr)), 2),
            max_ms=round(float(np.max(arr)), 2),
            mean_ms=round(float(np.mean(arr)), 2),
        )

    def to_dict(self) -> dict[str, float]:
        return {
            "p50_ms": self.p50_ms,
            "p90_ms": self.p90_ms,
            "p95_ms": self.p95_ms,
            "p99_ms": self.p99_ms,
            "min_ms": self.min_ms,
            "max_ms": self.max_ms,
            "mean_ms": self.mean_ms,
        }


@dataclass
class LoadTestReport:
    """Complete measured report for §23 acceptance criteria.

    Throughput, p50/p95/p99, zero duplicates, zero drops, fail-open under an injected outage.
    """

    mode: str
    units_requested: int
    units_completed: int
    units_pending: int
    units_needs_attention: int
    units_failed: int
    concurrency: int
    latency_profile: str
    total_duration_s: float
    throughput_units_per_s: float
    throughput_units_per_min: float
    latency: LatencyPercentiles
    queue_wait_latency: LatencyPercentiles
    duplicates_count: int
    drops_count: int
    zero_duplicates_verified: bool
    zero_drops_verified: bool
    fail_open_verified: bool
    injected_outage: str | None = None
    spend_preflight: dict[str, Any] | None = None
    records: list[UnitExecutionRecord] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "units_requested": self.units_requested,
            "units_completed": self.units_completed,
            "units_pending": self.units_pending,
            "units_needs_attention": self.units_needs_attention,
            "units_failed": self.units_failed,
            "concurrency": self.concurrency,
            "latency_profile": self.latency_profile,
            "total_duration_s": round(self.total_duration_s, 3),
            "throughput_units_per_s": round(self.throughput_units_per_s, 2),
            "throughput_units_per_min": round(self.throughput_units_per_min, 1),
            "latency_ms": self.latency.to_dict(),
            "queue_wait_latency_ms": self.queue_wait_latency.to_dict(),
            "duplicates_count": self.duplicates_count,
            "drops_count": self.drops_count,
            "zero_duplicates_verified": self.zero_duplicates_verified,
            "zero_drops_verified": self.zero_drops_verified,
            "fail_open_verified": self.fail_open_verified,
            "injected_outage": self.injected_outage,
            "spend_preflight": self.spend_preflight,
            "records": [r.to_dict() for r in self.records],
        }

    def to_markdown(self) -> str:
        dup_status = (
            "PASS (0 duplicates)"
            if self.zero_duplicates_verified
            else f"FAIL ({self.duplicates_count} duplicates)"
        )
        drop_status = "PASS (0 drops)" if self.zero_drops_verified else f"FAIL ({self.drops_count} drops)"
        fail_open_status = "PASS (fail-open verified)" if self.fail_open_verified else "FAIL"

        mean_str = (
            f"- **Mean:** {self.latency.mean_ms:.2f} ms "
            f"(min: {self.latency.min_ms:.2f} ms, max: {self.latency.max_ms:.2f} ms)"
        )

        lines = [
            "# Load and Resilience Test Report (§23)",
            "",
            f"- **Mode:** `{self.mode}`",
            f"- **Units Requested:** {self.units_requested}",
            f"- **Units Completed:** {self.units_completed}",
            f"- **Units Pending:** {self.units_pending}",
            f"- **Units Needs Attention:** {self.units_needs_attention}",
            f"- **Concurrency:** {self.concurrency}",
            f"- **Latency Profile:** `{self.latency_profile}`",
            f"- **Total Duration:** {self.total_duration_s:.3f} s",
            (
                f"- **Throughput:** {self.throughput_units_per_s:.2f} units/s "
                f"({self.throughput_units_per_min:.1f} units/min)"
            ),
            "",
            "## Latency Distribution (End-to-End)",
            f"- **p50:** {self.latency.p50_ms:.2f} ms",
            f"- **p90:** {self.latency.p90_ms:.2f} ms",
            f"- **p95:** {self.latency.p95_ms:.2f} ms",
            f"- **p99:** {self.latency.p99_ms:.2f} ms",
            mean_str,
            "",
            "## Queue Wait Latency",
            f"- **p50:** {self.queue_wait_latency.p50_ms:.2f} ms",
            f"- **p95:** {self.queue_wait_latency.p95_ms:.2f} ms",
            f"- **Mean:** {self.queue_wait_latency.mean_ms:.2f} ms",
            "",
            "## Acceptance Criteria (§23)",
            f"- **Zero Duplicates:** {dup_status}",
            f"- **Zero Drops:** {drop_status}",
            f"- **Fail-Open Under Outage:** {fail_open_status}",
        ]
        if self.injected_outage:
            lines.append(f"- **Injected Outage:** `{self.injected_outage}`")
        if self.spend_preflight:
            lines.extend(
                [
                    "",
                    "## Spend Preflight Guard (§18.4)",
                    f"- **Model ID:** `{self.spend_preflight.get('model_id')}`",
                    f"- **Estimated Cost:** ${self.spend_preflight.get('estimated_cost_usd')}",
                    f"- **Requests Remaining Today:** {self.spend_preflight.get('requests_remaining_today')}",
                ]
            )
        return "\n".join(lines)


@dataclass(frozen=True)
class DrillReport:
    """Report for an individual resilience drill."""

    drill_name: str
    passed: bool
    details: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_markdown(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        lines = [
            f"### Drill: {self.drill_name} [{status}]",
            "",
        ]
        for k, v in self.details.items():
            lines.append(f"- **{k}:** {v}")
        return "\n".join(lines)
