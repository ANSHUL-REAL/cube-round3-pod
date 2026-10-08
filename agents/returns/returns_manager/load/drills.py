"""Resilience and outage drills (§19, §23).

Builds on existing jobs/circuit.py, security/controls.py, and jobs/budget.py
to verify fail-open guarantees, circuit breaker behavior, kill switches, and
budget holds without reinventing them.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from returns_manager.config import Settings
from returns_manager.db.pool import Database
from returns_manager.ids import new_id
from returns_manager.intake.service import IntakeService
from returns_manager.jobs.budget import check_and_reserve_budget
from returns_manager.jobs.circuit import CircuitBreaker, CircuitState
from returns_manager.jobs.queue import JobQueue, JobRecord
from returns_manager.jobs.statemachine import JobStatus, ReturnStatus
from returns_manager.jobs.worker import HandlerResult, Worker
from returns_manager.load.models import DrillReport
from returns_manager.load.runner import generate_test_jpeg, run_load_test
from returns_manager.security import controls
from returns_manager.security.controls import Control


async def run_burst_scenario(
    db: Database,
    settings: Settings,
    *,
    n_units: int = 20,
    concurrency: int = 4,
) -> DrillReport:
    """Burst scenario drill (§23): sudden spike of units enqueued simultaneously."""
    report = await run_load_test(
        db,
        settings,
        mode="replay",
        units=n_units,
        concurrency=concurrency,
        latency_profile="instant",
    )

    passed = (
        report.zero_duplicates_verified and report.zero_drops_verified and report.units_completed == n_units
    )
    details = {
        "units": n_units,
        "concurrency": concurrency,
        "throughput_units_per_s": report.throughput_units_per_s,
        "p50_latency_ms": report.latency.p50_ms,
        "p95_latency_ms": report.latency.p95_ms,
        "zero_duplicates": report.zero_duplicates_verified,
        "zero_drops": report.zero_drops_verified,
    }
    return DrillReport(drill_name="burst_scenario", passed=passed, details=details)


async def run_outage_drill(
    db: Database,
    settings: Settings,
    *,
    n_units: int = 10,
    concurrency: int = 2,
    outage_type: str = "provider_500",
) -> DrillReport:
    """Injected outage drill (§23): verifies fail-open, zero duplicates, zero drops."""
    report = await run_load_test(
        db,
        settings,
        mode="replay",
        units=n_units,
        concurrency=concurrency,
        injected_outage=outage_type,
    )

    passed = report.fail_open_verified and report.zero_duplicates_verified and report.zero_drops_verified
    details = {
        "outage_type": outage_type,
        "units": n_units,
        "fail_open_verified": report.fail_open_verified,
        "units_pending": report.units_pending,
        "units_needs_attention": report.units_needs_attention,
        "zero_duplicates": report.zero_duplicates_verified,
        "zero_drops": report.zero_drops_verified,
    }
    return DrillReport(drill_name="injected_outage", passed=passed, details=details)


async def run_kill_switch_drill(
    db: Database,
    settings: Settings,
) -> DrillReport:
    """Kill-switch drill (§6.7, §23): toggle model_calls_enabled and auto_disposition_enabled."""
    org_id = f"org_t{uuid.uuid4().hex[:12]}"
    async with db.transaction(org_id) as conn:
        await conn.execute(
            "INSERT INTO rm.organizations (org_id, name) VALUES (%s, %s) ON CONFLICT (org_id) DO NOTHING",
            (org_id, f"Kill Switch Org {org_id}"),
        )

    # 1. Flip model_calls_enabled OFF globally
    await controls.set_global_control(
        db, Control.MODEL_CALLS, enabled=False, reason="drill test model calls off", actor="drill"
    )

    intake = IntakeService(db)
    ret = await intake.create_return(org_id=org_id, actor_id="op", order_id="ORD-KILL-1", unit_id="U-KILL-01")
    await intake.upload_photo(
        org_id=org_id, actor_id="op", return_id=ret.return_id, photo_bytes=generate_test_jpeg(1)
    )

    async with db.transaction(org_id) as conn:
        job = await JobQueue.enqueue_job(conn, org_id, ret.return_id, idempotency_key=f"k-{ret.return_id}")

    # Worker attempts job while kill-switch is active
    called = []

    async def h(j: JobRecord) -> HandlerResult:
        called.append(j.job_id)
        return HandlerResult(target_return_status=ReturnStatus.AWAITING_OPERATOR)

    worker = Worker(db, settings, concurrency=1, kinds=["judgment"], handler=h)
    task = asyncio.create_task(worker.run())
    await asyncio.sleep(0.5)
    await worker.stop()
    await task

    # Verify job stayed pending/retryable without spending attempts (§10.4, §10.6)
    async with db.transaction(org_id) as conn:
        job_after = await JobQueue.get_job(conn, org_id, job.job_id)
        assert job_after is not None
        held_pending = job_after.status == JobStatus.FAILED_RETRYABLE
        attempts_not_spent = job_after.attempts == 0
        reason_set = job_after.last_error_class == "model_calls_disabled"

    # 2. Re-enable model_calls_enabled globally
    await controls.set_global_control(
        db, Control.MODEL_CALLS, enabled=True, reason="drill test restore", actor="drill"
    )

    # Make due now and verify job resumes and finishes
    async with db.transaction(org_id) as conn:
        await conn.execute(
            "UPDATE rm.inspection_jobs SET next_attempt_at = now() WHERE org_id = %s AND job_id = %s",
            (org_id, job.job_id),
        )

    worker2 = Worker(db, settings, concurrency=1, kinds=["judgment"], handler=h)
    await worker2.run(max_jobs=1)

    async with db.transaction(org_id) as conn:
        job_final = await JobQueue.get_job(conn, org_id, job.job_id)
        assert job_final is not None
        succeeded = job_final.status == JobStatus.SUCCEEDED

    passed = held_pending and attempts_not_spent and reason_set and succeeded
    details = {
        "held_pending": held_pending,
        "attempts_not_spent": attempts_not_spent,
        "reason_set": reason_set,
        "resumed_and_succeeded": succeeded,
    }
    return DrillReport(drill_name="kill_switch", passed=passed, details=details)


async def run_circuit_breaker_drill(
    db: Database,
    settings: Settings,
) -> DrillReport:
    """Circuit breaker drill (§10.4, §23): threshold failures open circuit, cooldown probe."""
    cb = CircuitBreaker("gemini-test-circuit", failure_threshold=3, cooldown_s=2.0)
    t0 = datetime(2026, 9, 26, 12, 0, 0, tzinfo=UTC)

    # 1. Closed initially
    initial_closed = cb.state == CircuitState.CLOSED and cb.allow_request(now=t0)

    # 2. 3 failures trip to OPEN
    cb.record_failure(counts_toward_circuit=True, now=t0)
    cb.record_failure(counts_toward_circuit=True, now=t0)
    cb.record_failure(counts_toward_circuit=True, now=t0)
    tripped_open = cb.state == CircuitState.OPEN and not cb.allow_request(now=t0 + timedelta(seconds=1.0))

    # 3. Cooldown expires -> transitions to HALF_OPEN probe
    t_cooldown = t0 + timedelta(seconds=2.1)
    probe_allowed = cb.allow_request(now=t_cooldown)
    is_half_open = cb.state == CircuitState.HALF_OPEN

    # 4. Successful probe closes circuit
    cb.record_success()
    recovered_closed = cb.state == CircuitState.CLOSED and cb.allow_request(now=t_cooldown)

    passed = initial_closed and tripped_open and probe_allowed and is_half_open and recovered_closed
    details = {
        "initial_closed": initial_closed,
        "tripped_open": tripped_open,
        "probe_allowed": probe_allowed,
        "is_half_open": is_half_open,
        "recovered_closed": recovered_closed,
    }
    return DrillReport(drill_name="circuit_breaker", passed=passed, details=details)


async def run_budget_exhaustion_drill(
    db: Database,
    settings: Settings,
) -> DrillReport:
    """Budget exhaustion drill (§10.4a, §23): quota limits hold jobs pending without burning attempts."""
    org_id = f"org_t{uuid.uuid4().hex[:12]}"
    async with db.transaction(org_id) as conn:
        await conn.execute(
            "INSERT INTO rm.organizations (org_id, name) VALUES (%s, %s) ON CONFLICT (org_id) DO NOTHING",
            (org_id, f"Budget Drill Org {org_id}"),
        )

    # Reserve the entire daily budget for a test model
    test_model = f"gemini-drill-{new_id()[:6]}"
    async with db.transaction(None) as conn:
        await check_and_reserve_budget(conn, test_model, daily_budget=5, count=5)
        # Check that subsequent reservation is denied
        st = await check_and_reserve_budget(conn, test_model, daily_budget=5, count=1)
        quota_denied = not st.allowed and st.requests_remaining == 0

    passed = quota_denied
    details = {
        "test_model": test_model,
        "quota_denied_when_budget_exhausted": quota_denied,
        "requests_remaining": st.requests_remaining,
    }
    return DrillReport(drill_name="budget_exhaustion", passed=passed, details=details)


async def run_all_drills(
    db: Database,
    settings: Settings,
) -> list[DrillReport]:
    """Execute all resilience drills and return individual reports."""
    reports: list[DrillReport] = []
    reports.append(await run_burst_scenario(db, settings, n_units=12, concurrency=3))
    reports.append(await run_outage_drill(db, settings, n_units=6, concurrency=2))
    reports.append(await run_kill_switch_drill(db, settings))
    reports.append(await run_circuit_breaker_drill(db, settings))
    reports.append(await run_budget_exhaustion_drill(db, settings))
    return reports
