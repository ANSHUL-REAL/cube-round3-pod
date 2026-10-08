"""Load test runner for replay and live modes (§19, §20, §23).

Measures throughput, p50/p95/p99 latency, zero duplicates, zero drops,
and fail-open behavior under injected outages.
"""

from __future__ import annotations

import asyncio
import io
import logging
import uuid
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import numpy as np
from PIL import Image

from returns_manager.config import Settings
from returns_manager.db.pool import Database
from returns_manager.errors import BadRequest
from returns_manager.ids import new_id
from returns_manager.intake.service import IntakeService
from returns_manager.jobs.budget import get_budget_status
from returns_manager.jobs.queue import JobQueue, JobRecord
from returns_manager.jobs.statemachine import JobStatus, ReturnStatus
from returns_manager.jobs.worker import HandlerResult, JobHandler, Worker
from returns_manager.llm.client import ProviderError
from returns_manager.load.models import (
    LatencyPercentiles,
    LatencyProfile,
    LoadMode,
    LoadTestReport,
    UnitExecutionRecord,
)
from returns_manager.load.profiles import parse_latency_profile
from returns_manager.observability.spend_guard import preflight_bulk_spend

logger = logging.getLogger(__name__)


def generate_test_jpeg(seed: int, size: tuple[int, int] = (600, 450)) -> bytes:
    """Generate a photo with sufficient texture and contrast to pass the quality gate."""
    rng = np.random.default_rng(seed)
    block_h = max(2, size[1] // 30)
    block_w = max(2, size[0] // 30)
    blocks = rng.integers(50, 200, size=(size[1] // block_h, size[0] // block_w, 3), dtype=np.uint8)
    buf = io.BytesIO()
    Image.fromarray(np.kron(blocks, np.ones((block_h, block_w, 1), dtype=np.uint8)), "RGB").save(
        buf, "JPEG", quality=85
    )
    return buf.getvalue()


async def run_load_test(
    db: Database,
    settings: Settings,
    *,
    mode: str = "replay",
    units: int = 10,
    concurrency: int = 4,
    latency_profile: str | LatencyProfile | None = "instant",
    confirm_spend: bool = False,
    allow_multi_day: bool = False,
    injected_outage: str | None = None,
    org_id: str | None = None,
    timeout_s: float = 60.0,
) -> LoadTestReport:
    """Execute a load test in replay or live mode (§20).

    Replay mode:
      - Simulates model delays using the specified latency profile.
      - Measures system queue, worker, and database throughput without model quota.

    Live mode:
      - Requires preflight spend check (§18.4) via `preflight_bulk_spend`.
      - Refuses (SpendGuardRefused, exit 4) if quota/cost cap is exceeded and not confirmed.

    Acceptance criteria (§23):
      - Measured report: throughput, p50/p95/p99.
      - Zero duplicates: no job executed multiple times.
      - Zero drops: every submitted unit accounted for.
      - Fail-open under an injected outage: photos intact, return pending/needs_attention.
    """
    if units <= 0:
        raise BadRequest("units must be > 0")
    if concurrency <= 0:
        raise BadRequest("concurrency must be > 0")

    profile = (
        latency_profile
        if isinstance(latency_profile, LatencyProfile)
        else parse_latency_profile(latency_profile)
    )

    spend_preflight_dict: dict[str, Any] | None = None

    # ── Live mode spend preflight guard (§18.4, §20) ─────────────────────────
    if mode == LoadMode.LIVE:
        async with db.transaction(None) as conn:
            budget_status = await get_budget_status(
                conn, settings.rm_judgment_model, settings.rm_daily_request_budget_judgment
            )

        preflight = preflight_bulk_spend(
            model_id=settings.rm_judgment_model,
            units=units,
            expected_requests_per_unit=1.2,
            estimated_cost_usd_per_unit=Decimal("0.0015"),
            budget_status=budget_status,
            spend_cap_usd=Decimal("0.001"),  # Nominal cap requiring explicit --confirm-spend
            allow_multi_day=allow_multi_day,
            confirm_spend=confirm_spend,
        )
        spend_preflight_dict = preflight.to_dict()

    # ── Setup tenant org ─────────────────────────────────────────────────────
    test_org = org_id or f"org_t{uuid.uuid4().hex[:12]}"
    async with db.transaction(test_org) as conn:
        await conn.execute(
            """INSERT INTO rm.organizations (org_id, name)
               VALUES (%s, %s)
               ON CONFLICT (org_id) DO NOTHING""",
            (test_org, f"Load Test Org {test_org}"),
        )

    # ── Enqueue test workload ────────────────────────────────────────────────
    intake = IntakeService(db)
    unit_records: dict[str, UnitExecutionRecord] = {}
    return_ids: list[str] = []

    logger.info("Enqueuing %d units for load test (org=%s, mode=%s)...", units, test_org, mode)
    for i in range(units):
        unit_id = f"U-LOAD-{new_id()[:6]}-{i + 1:04d}"
        order_id = f"ORD-LOAD-{i + 1:04d}"
        ret = await intake.create_return(
            org_id=test_org,
            actor_id="load_tester",
            order_id=order_id,
            unit_id=unit_id,
        )
        return_ids.append(ret.return_id)

        # Upload valid photo
        photo_bytes = generate_test_jpeg(100 + i)
        await intake.upload_photo(
            org_id=test_org,
            actor_id="load_tester",
            return_id=ret.return_id,
            photo_bytes=photo_bytes,
        )

        t_enqueued = datetime.now(UTC)
        async with db.transaction(test_org) as conn:
            job = await JobQueue.enqueue_job(
                conn,
                test_org,
                ret.return_id,
                idempotency_key=f"load-{ret.return_id}",
            )

        unit_records[job.job_id] = UnitExecutionRecord(
            unit_id=unit_id,
            return_id=ret.return_id,
            job_id=job.job_id,
            enqueued_at=t_enqueued,
        )

    # ── Worker and execution tracking ─────────────────────────────────────────
    executions: Counter[str] = Counter()
    t_start = datetime.now(UTC)

    async def synthetic_handler(job: JobRecord) -> HandlerResult:
        """Replay mode: no model call, ever. Measures queue/worker/DB throughput and
        (with `injected_outage`) the worker's own fail-open behavior - never real
        latency or real cost, and never gated by real production quota/kill-switch/
        circuit state (see `bypass_operational_guards` below)."""
        executions[job.job_id] += 1
        rec = unit_records.get(job.job_id)
        if rec:
            rec.claimed_at = datetime.now(UTC)

        # Handle injected outage if active (§23)
        if injected_outage == "provider_500":
            if rec:
                rec.error_class = "server_error"
                rec.error_detail = "Simulated 500 internal server error from model provider"
            raise ProviderError(
                "server_error", "500 internal server error from model provider", status_code=500
            )

        if injected_outage == "timeout":
            if rec:
                rec.error_class = "timeout"
                rec.error_detail = "Simulated request timeout from model provider"
            raise TimeoutError("Model provider request timed out")

        # Simulate model latency profile
        delay_s = profile.sample_delay_s()
        if delay_s > 0:
            await asyncio.sleep(delay_s)

        if rec:
            rec.completed_at = datetime.now(UTC)
            rec.status = "succeeded"
            rec.finalize_timings()

        return HandlerResult(target_return_status=ReturnStatus.AWAITING_OPERATOR)

    is_live = mode == LoadMode.LIVE

    if is_live:
        if injected_outage:
            raise BadRequest("--injected-outage is only meaningful in replay mode (--mode replay)")
        # Live mode must call the SAME handler real inspections use, so "real latency
        # and cost" (§20) means something - measuring the synthetic handler's simulated
        # delay under a different flag name would just be replay mode wearing a live
        # label. This also means live mode is subject to the real quota/kill-switch/
        # circuit guards in jobs/worker.py, exactly like production traffic - that is
        # the point, not a bug: §18.4's spend guard above is the pre-flight estimate,
        # these are the real-time enforcement.
        from returns_manager.inspection.runtime import build_runtime

        real_handler = build_runtime(db, settings).handler

        async def live_handler(job: JobRecord) -> HandlerResult:
            executions[job.job_id] += 1
            rec = unit_records.get(job.job_id)
            if rec:
                rec.claimed_at = datetime.now(UTC)
            try:
                result = await real_handler(job)
            except Exception as exc:
                if rec:
                    rec.error_class = type(exc).__name__
                    rec.error_detail = str(exc)[:500]
                raise
            if rec:
                rec.completed_at = datetime.now(UTC)
                rec.status = "succeeded"
                rec.finalize_timings()
            return result

        job_handler: JobHandler = live_handler
    else:
        job_handler = synthetic_handler

    # Launch worker pool
    worker_settings = settings.model_copy(update={"rm_worker_concurrency": 1})
    workers = [
        Worker(
            db,
            worker_settings,
            concurrency=1,
            kinds=["judgment"],
            handler=job_handler,
            bypass_operational_guards=not is_live,
        )
        for _ in range(concurrency)
    ]
    worker_tasks = [asyncio.create_task(w.run()) for w in workers]

    # Poll until all jobs are processed or timeout
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        async with db.transaction(test_org) as conn:
            cur = await conn.execute(
                """SELECT status, count(*) AS cnt
                   FROM rm.inspection_jobs
                   WHERE org_id = %s AND job_id = ANY(%s)
                   GROUP BY status""",
                (test_org, list(unit_records.keys())),
            )
            rows = await cur.fetchall()
            status_counts = {r["status"]: int(r["cnt"]) for r in rows}
            in_flight = status_counts.get("in_progress", 0) + status_counts.get("pending", 0)
            if in_flight == 0:
                break
        await asyncio.sleep(0.05)

    # Stop workers gracefully
    for w in workers:
        await w.stop()
    await asyncio.gather(*worker_tasks)

    t_end = datetime.now(UTC)
    total_duration_s = max(0.001, (t_end - t_start).total_seconds())

    # ── Database state analysis & verification ────────────────────────────────
    async with db.transaction(test_org) as conn:
        # Check job final statuses and attempts
        cur = await conn.execute(
            """SELECT job_id, status, attempts, last_error_class, last_error_detail
               FROM rm.inspection_jobs
               WHERE org_id = %s AND job_id = ANY(%s)""",
            (test_org, list(unit_records.keys())),
        )
        final_jobs = {r["job_id"]: r for r in await cur.fetchall()}

        # Check return statuses
        cur = await conn.execute(
            """SELECT return_id, status
               FROM rm.returns
               WHERE org_id = %s AND return_id = ANY(%s)""",
            (test_org, return_ids),
        )
        final_returns = {r["return_id"]: r for r in await cur.fetchall()}

        # Check photo counts intact
        cur = await conn.execute(
            """SELECT return_id, count(*) AS photo_count
               FROM rm.return_photos
               WHERE org_id = %s AND return_id = ANY(%s)
               GROUP BY return_id""",
            (test_org, return_ids),
        )
        final_photos = {r["return_id"]: int(r["photo_count"]) for r in await cur.fetchall()}

    # Update records with final DB state
    units_completed = 0
    units_pending = 0
    units_needs_attention = 0
    units_failed = 0

    for job_id, rec in unit_records.items():
        job_row = final_jobs.get(job_id)
        if job_row:
            rec.status = str(job_row["status"])
            if job_row["last_error_class"]:
                rec.error_class = job_row["last_error_class"]
            if job_row["last_error_detail"]:
                rec.error_detail = job_row["last_error_detail"]

            if rec.status == JobStatus.SUCCEEDED:
                units_completed += 1
            elif rec.status in (JobStatus.PENDING, JobStatus.FAILED_RETRYABLE):
                units_pending += 1
            elif rec.status == JobStatus.NEEDS_ATTENTION:
                units_needs_attention += 1
            else:
                units_failed += 1

        if rec.completed_at is None and rec.claimed_at is not None:
            rec.completed_at = t_end
            rec.finalize_timings()

    # ── Acceptance checks (§23) ───────────────────────────────────────────────
    # Zero duplicates: no job executed multiple times
    duplicates_count = sum(max(0, count - 1) for count in executions.values())
    zero_duplicates_verified = duplicates_count == 0

    # Zero drops: all submitted units accounted for in final returns
    drops_count = len(return_ids) - len(final_returns)
    zero_drops_verified = drops_count == 0

    # Fail-open under injected outage:
    # 1. Photos remain 100% intact (every return has its photo)
    # 2. Returns remain pending or needs_attention, never auto-disposed or guessed
    # 3. No unhandled crashes
    fail_open_verified = True
    if injected_outage:
        all_photos_intact = all(final_photos.get(rid, 0) >= 1 for rid in return_ids)
        no_auto_disposition = all(
            r["status"] in ("pending", "needs_attention", "queued", "awaiting_operator")
            for r in final_returns.values()
        )
        fail_open_verified = all_photos_intact and no_auto_disposition
    else:
        all_photos_intact = all(final_photos.get(rid, 0) >= 1 for rid in return_ids)
        fail_open_verified = all_photos_intact

    # ── Compute latency percentiles and throughput ────────────────────────────
    total_latencies = [rec.total_latency_ms for rec in unit_records.values() if rec.total_latency_ms > 0]
    queue_wait_latencies = [rec.queue_wait_ms for rec in unit_records.values() if rec.queue_wait_ms > 0]

    latency_percentiles = LatencyPercentiles.compute(total_latencies)
    queue_percentiles = LatencyPercentiles.compute(queue_wait_latencies)

    throughput_units_per_s = units_completed / total_duration_s
    throughput_units_per_min = throughput_units_per_s * 60.0

    return LoadTestReport(
        mode=mode,
        units_requested=units,
        units_completed=units_completed,
        units_pending=units_pending,
        units_needs_attention=units_needs_attention,
        units_failed=units_failed,
        concurrency=concurrency,
        latency_profile=profile.raw,
        total_duration_s=total_duration_s,
        throughput_units_per_s=throughput_units_per_s,
        throughput_units_per_min=throughput_units_per_min,
        latency=latency_percentiles,
        queue_wait_latency=queue_percentiles,
        duplicates_count=duplicates_count,
        drops_count=drops_count,
        zero_duplicates_verified=zero_duplicates_verified,
        zero_drops_verified=zero_drops_verified,
        fail_open_verified=fail_open_verified,
        injected_outage=injected_outage,
        spend_preflight=spend_preflight_dict,
        records=list(unit_records.values()),
    )
