"""Durable job worker loop, heartbeats, concurrency, and fail-open guarantees (§10.2, §10.6).

The worker never makes a decision by itself. A job's handler (the Judgment pipeline, wired in P5) does the
work; without a configured handler every job ends in `needs_attention` with the photos intact, never in a
decision-ready state. Operational holds (kill switch, open circuit, exhausted daily quota) put the job back
without consuming one of its attempts.
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from returns_manager import __version__
from returns_manager.config import Settings
from returns_manager.db.pool import Database
from returns_manager.errors import ConfigError, HandlerNotConfigured, InvalidTransitionError
from returns_manager.ids import new_id
from returns_manager.jobs.budget import get_budget_status
from returns_manager.jobs.circuit import CircuitBreakerRegistry
from returns_manager.jobs.queue import JobQueue, JobRecord
from returns_manager.jobs.retry import classify_error, compute_next_attempt_at
from returns_manager.jobs.statemachine import ReturnStatus, transition_return_status
from returns_manager.security import controls

logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_S = 30.0
IDLE_POLL_S = 0.5


def assert_lease_sizing(settings: Settings) -> None:
    """Assert that RM_JOB_LEASE_S exceeds worst-case processing time (§10.2)."""
    worst_case_s = settings.rm_model_timeout_s * settings.rm_max_round_trips
    if settings.rm_job_lease_s <= worst_case_s:
        raise ConfigError(
            f"RM_JOB_LEASE_S ({settings.rm_job_lease_s}s) must exceed worst-case "
            f"processing time ({worst_case_s}s = RM_MODEL_TIMEOUT_S * RM_MAX_ROUND_TRIPS)"
        )


Persist = Callable[[Any], Awaitable[None]]


@dataclass
class HandlerResult:
    """What a handler produced. `persist(conn)` runs in the SAME transaction as the return's state
    transition and the job's success mark (§11.7 step 12), so results and state can never diverge. The
    handler itself runs outside any transaction: a model call must not pin a pooled connection."""

    target_return_status: ReturnStatus
    persist: Persist | None = None
    details: dict[str, Any] | None = None


JobHandler = Callable[[JobRecord], Awaitable[HandlerResult]]

# Per-class attempt caps (§10.4): truncation is retried once, schema errors up to twice.
_CLASS_ATTEMPT_CAP = {"truncated": 2, "schema_error": 3}


async def _no_handler(job: JobRecord) -> HandlerResult:
    raise HandlerNotConfigured(
        f"no job handler is configured for kind '{job.kind}' (the Judgment Agent is built in P5)"
    )


async def _set_return_status(
    conn: Any, org_id: str, return_id: str, target: ReturnStatus, trigger: str
) -> ReturnStatus | None:
    """Move a return through the state machine. Returns the new status, or None if the row is missing."""
    cur = await conn.execute(
        "SELECT status FROM rm.returns WHERE org_id = %s AND return_id = %s FOR UPDATE",
        (org_id, return_id),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    if row["status"] == str(target):
        return target
    new_status = transition_return_status(row["status"], target, trigger=trigger)
    await conn.execute(
        "UPDATE rm.returns SET status = %s WHERE org_id = %s AND return_id = %s",
        (str(new_status), org_id, return_id),
    )
    return new_status


class Worker:
    """Asyncio background worker processing durable inspection jobs (§10.2)."""

    def __init__(
        self,
        db: Database,
        settings: Settings,
        worker_id: str | None = None,
        concurrency: int | None = None,
        kinds: list[str] | None = None,
        handler: JobHandler | None = None,
        breakers: CircuitBreakerRegistry | None = None,
        bypass_operational_guards: bool = False,
    ) -> None:
        assert_lease_sizing(settings)
        self.db = db
        self.settings = settings
        self.worker_id = worker_id or f"wrk_{socket.gethostname()}_{os.getpid()}_{new_id()[:8]}"
        self.concurrency = concurrency or settings.rm_worker_concurrency
        self.kinds = kinds or ["judgment", "escalation", "reinspection"]
        self.handler: JobHandler = handler or _no_handler
        self.breakers = breakers or CircuitBreakerRegistry(
            failure_threshold=settings.rm_circuit_failure_threshold,
            cooldown_s=settings.rm_circuit_cooldown_s,
        )
        # Skips the kill-switch, circuit-breaker and daily-quota checks in _process_job
        # (steps 1-3) before every job. Off by default: production code must never set
        # this. It exists only for load-test's synthetic replay-mode throughput
        # measurement (load/runner.py), whose handler never calls a model and so has
        # nothing to do with real production quota/circuit/kill-switch state - without
        # it, a load-test run is spuriously gated by whatever real quota happens to be
        # left today. The dedicated kill-switch/circuit/budget drills (load/drills.py)
        # never set this: their entire purpose is to exercise these real guards.
        self.bypass_operational_guards = bypass_operational_guards

        self.processed = 0
        self._max_jobs: int | None = None
        self._stopping = False
        self._done = asyncio.Event()
        self._active_jobs: dict[str, JobRecord] = {}
        self._slot_tasks: list[asyncio.Task[None]] = []
        self._heartbeat_task: asyncio.Task[None] | None = None

    async def run(self, max_jobs: int | None = None) -> int:
        """Run until stop() is called, or until this worker has finished `max_jobs` jobs.

        Returns the number of jobs this worker finished (holds put back in the queue do not count).
        """
        self._stopping = False
        self._done.clear()
        self._max_jobs = max_jobs
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        self._slot_tasks = [asyncio.create_task(self._worker_slot(i)) for i in range(self.concurrency)]
        try:
            await self._done.wait()
        finally:
            await self.stop()
        return self.processed

    async def stop(self, grace_period_s: float = 3.0) -> None:
        """Graceful shutdown (§10.2): stop claiming, let in-flight jobs finish, else release their leases."""
        self._stopping = True
        self._done.set()
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()

        pending = [t for t in self._slot_tasks if not t.done()]
        if pending:
            _, still_running = await asyncio.wait(pending, timeout=grace_period_s)
            # Snapshot before cancelling: a cancelled slot drops its job from _active_jobs.
            unfinished = list(self._active_jobs.values())
            for t in still_running:
                t.cancel()
            await asyncio.gather(*still_running, return_exceptions=True)
            for job in unfinished:
                await self._release(job, "shutdown", "Worker shut down while the job was in flight")

    def _count_finished(self) -> None:
        self.processed += 1
        if self._max_jobs is not None and self.processed >= self._max_jobs:
            self._stopping = True
            self._done.set()

    async def _release(self, job: JobRecord, error_class: str, detail: str) -> None:
        try:
            async with self.db.transaction(job.org_id) as conn:
                await JobQueue.mark_job_failed_retryable(
                    conn, job.org_id, job.job_id, error_class, detail, datetime.now(UTC)
                )
        except Exception as exc:
            logger.error("Failed to release job %s: %s", job.job_id, exc)

    async def _heartbeat_loop(self) -> None:
        """Update worker_heartbeats, reap dead workers' leases and renew our own leases (§10.2)."""
        while not self._stopping:
            try:
                async with self.db.transaction(None) as conn:
                    await conn.execute(
                        """
                        INSERT INTO rm.worker_heartbeats (
                            worker_id, host, pid, version, concurrency, started_at, last_seen_at
                        ) VALUES (%s, %s, %s, %s, %s, now(), now())
                        ON CONFLICT (worker_id) DO UPDATE SET last_seen_at = now()
                        """,
                        (self.worker_id, platform.node(), os.getpid(), __version__, self.concurrency),
                    )
                    reaped = await JobQueue.reap_expired_leases(conn, max_rows=50)
                    if reaped > 0:
                        logger.info("Reaped %d expired worker leases", reaped)

                for job in list(self._active_jobs.values()):
                    async with self.db.transaction(job.org_id) as conn:
                        renewed = await JobQueue.renew_lease(
                            conn, job.org_id, job.job_id, self.worker_id, self.settings.rm_job_lease_s
                        )
                    if not renewed:
                        logger.warning("Lease for job %s was lost (reaped or reassigned)", job.job_id)
            except Exception as exc:
                logger.error("Error in worker heartbeat: %s", exc)

            try:
                await asyncio.sleep(HEARTBEAT_INTERVAL_S)
            except asyncio.CancelledError:
                break

    async def _worker_slot(self, slot_idx: int) -> None:
        """One concurrency slot: claim, process, repeat."""
        while not self._stopping:
            try:
                async with self.db.transaction(None) as conn:
                    job = await JobQueue.claim_next_job(
                        conn,
                        worker_id=self.worker_id,
                        lease_s=self.settings.rm_job_lease_s,
                        max_inflight_per_org=self.settings.rm_max_inflight_per_org,
                    )
                if job is None:
                    await asyncio.sleep(IDLE_POLL_S)
                    continue

                if job.kind not in self.kinds:
                    await self._hold(
                        job, "unhandled_kind", f"Worker does not handle job kind '{job.kind}'", 60
                    )
                    continue

                self._active_jobs[job.job_id] = job
                try:
                    finished = await self._process_job(job)
                finally:
                    self._active_jobs.pop(job.job_id, None)
                if finished:
                    self._count_finished()

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Worker slot %d unexpected error: %s", slot_idx, exc)
                await asyncio.sleep(1.0)

    async def _hold(self, job: JobRecord, error_class: str, detail: str, delay_s: float) -> None:
        """Put a job back without spending an attempt (kill switch, circuit, quota: §10.4)."""
        async with self.db.transaction(job.org_id) as conn:
            await JobQueue.hold_job(
                conn,
                job.org_id,
                job.job_id,
                error_class,
                detail,
                datetime.now(UTC) + timedelta(seconds=delay_s),
            )

    async def _process_job(self, job: JobRecord) -> bool:
        """Process a claimed job, failing open (§10.2, §10.6). Returns True when the job finished."""
        if job.kind == "audit":
            model_id = self.settings.rm_audit_model
            daily_budget = self.settings.rm_daily_request_budget_audit
            kill_control = controls.Control.AUDIT
        elif job.kind == "escalation":
            model_id = self.settings.rm_escalation_model
            daily_budget = self.settings.rm_daily_request_budget_judgment
            kill_control = controls.Control.ESCALATION
        else:
            model_id = self.settings.rm_judgment_model
            daily_budget = self.settings.rm_daily_request_budget_judgment
            kill_control = controls.Control.MODEL_CALLS
        breaker = self.breakers.get(model_id)

        # Steps 1-3 gate on real production model availability (kill switch, circuit,
        # daily quota). A bypassed worker's handler never calls a model, so none of
        # these mean anything to it; skip straight to claiming/state-transition/handler.
        if not self.bypass_operational_guards:
            # 1. Kill switch (§6.7): effective value = global AND org.
            eff = await controls.effective(self.db, job.org_id)
            if not eff[controls.Control.MODEL_CALLS].enabled or not eff[kill_control].enabled:
                await self._hold(
                    job,
                    "model_calls_disabled",
                    f"{kill_control.value} is switched off (kill switch)",
                    60,
                )
                return False

            # 2. Circuit breaker (§10.4).
            if not breaker.allow_request():
                await self._hold(
                    job,
                    "circuit_open",
                    f"Circuit breaker is open for model '{model_id}'",
                    breaker.cooldown_s,
                )
                return False

            # 3. Daily request budget (§10.4a): the model client reserves tokens per
            #    request; here the job is only held when nothing is left today, so it
            #    waits for the Pacific-midnight reset.
            async with self.db.transaction(None) as conn:
                budget = await get_budget_status(
                    conn,
                    model_id,
                    daily_budget,
                    self.settings.rm_quota_reset_tz,
                )
            if not budget.allowed:
                delay = (budget.next_reset_utc - datetime.now(UTC)).total_seconds()
                reset_iso = budget.next_reset_utc.isoformat()
                await self._hold(
                    job,
                    "quota_exhausted",
                    f"Daily request budget for {model_id} exhausted (resets at {reset_iso})",
                    max(delay, 1.0),
                )
                return False

        # 4. State transitions on claim (§10.1):
        #    Only judgment jobs transition the return from QUEUED -> INSPECTING.
        #    Escalation jobs run on returns in awaiting_review and keep them there while working.
        #    Audit jobs run on unfinalized or finalized returns and do not move them to inspecting.
        if job.kind == "judgment":
            try:
                async with self.db.transaction(job.org_id) as conn:
                    moved = await _set_return_status(
                        conn, job.org_id, job.return_id, ReturnStatus.INSPECTING, "job_claimed"
                    )
                    if moved is None:
                        raise InvalidTransitionError(
                            "return", None, str(ReturnStatus.INSPECTING), "job_claimed"
                        )
            except InvalidTransitionError as exc:
                logger.warning("Cancelling stale job %s: %s", job.job_id, exc)
                async with self.db.transaction(job.org_id) as conn:
                    await JobQueue.mark_job_cancelled(conn, job.org_id, job.job_id)
                return True
        elif job.kind == "escalation":
            async with self.db.transaction(job.org_id) as conn:
                cur = await conn.execute(
                    "SELECT status FROM rm.returns WHERE org_id = %s AND return_id = %s",
                    (job.org_id, job.return_id),
                )
                row = await cur.fetchone()
                if row is None or row["status"] != str(ReturnStatus.AWAITING_REVIEW):
                    logger.warning(
                        "Cancelling stale escalation job %s for return %s in status %s",
                        job.job_id,
                        job.return_id,
                        row["status"] if row else "missing",
                    )
                    await JobQueue.mark_job_cancelled(conn, job.org_id, job.job_id)
                    return True
        elif job.kind == "audit":
            async with self.db.transaction(job.org_id) as conn:
                cur = await conn.execute(
                    "SELECT status FROM rm.returns WHERE org_id = %s AND return_id = %s",
                    (job.org_id, job.return_id),
                )
                row = await cur.fetchone()
                if row is None or row["status"] == str(ReturnStatus.CAPTURING):
                    await JobQueue.mark_job_cancelled(conn, job.org_id, job.job_id)
                    return True

        # 5. The handler runs outside any transaction (a model call must not pin a pooled connection). Its
        #    results, the return's state transition and the job's success mark are then one transaction.
        try:
            result = await self.handler(job)
            async with self.db.transaction(job.org_id) as conn:
                if result.persist is not None:
                    await result.persist(conn)
                if job.kind != "audit":
                    await _set_return_status(
                        conn, job.org_id, job.return_id, result.target_return_status, "inspection_complete"
                    )
                await JobQueue.mark_job_succeeded(conn, job.org_id, job.job_id)
            breaker.record_success()
            return True
        except Exception as exc:
            return await self._fail(job, exc, breaker, model_id=model_id, daily_budget=daily_budget)

    async def _fail(
        self,
        job: JobRecord,
        exc: Exception,
        breaker: Any,
        model_id: str | None = None,
        daily_budget: int | None = None,
    ) -> bool:
        """Fail open (§10.6): the return stays visible as pending or needs_attention; photos are untouched.

        `wait` classes (daily quota, kill switch, spend budget) hold the job until the condition can clear,
        without spending an attempt. Returns True when the job finished (failed for good or scheduled
        retry)."""
        classification = classify_error(exc)
        breaker.record_failure(counts_toward_circuit=classification.counts_toward_circuit)
        logger.warning(
            "Job %s failed with %s (%s): %s",
            job.job_id,
            classification.error_class,
            classification.action,
            exc,
        )
        m_id = model_id or self.settings.rm_judgment_model
        d_bud = daily_budget or self.settings.rm_daily_request_budget_judgment
        if classification.action == "wait":
            if classification.error_class == "quota_exhausted":
                async with self.db.transaction(None) as conn:
                    budget = await get_budget_status(
                        conn,
                        m_id,
                        d_bud,
                        self.settings.rm_quota_reset_tz,
                    )
                delay = max((budget.next_reset_utc - datetime.now(UTC)).total_seconds(), 1.0)
            else:
                delay = 60.0
            await self._hold(job, classification.error_class, classification.detail, delay)
            if job.kind == "judgment":
                await self._return_to(job, ReturnStatus.PENDING)
            return False
        cap = min(job.max_attempts, _CLASS_ATTEMPT_CAP.get(classification.error_class, job.max_attempts))
        retry = classification.retryable and job.attempts < cap
        async with self.db.transaction(job.org_id) as conn:
            if retry:
                next_at = compute_next_attempt_at(job.attempts)
                if classification.suggested_wait_s:  # e.g. a per-minute 429 carrying a retry delay
                    next_at = max(
                        next_at, datetime.now(UTC) + timedelta(seconds=classification.suggested_wait_s)
                    )
                await JobQueue.mark_job_failed_retryable(
                    conn, job.org_id, job.job_id, classification.error_class, classification.detail, next_at
                )
            else:
                await JobQueue.mark_job_needs_attention(
                    conn, job.org_id, job.job_id, classification.error_class, classification.detail
                )
        if job.kind == "judgment":
            await self._return_to(job, ReturnStatus.PENDING if retry else ReturnStatus.NEEDS_ATTENTION)
        return True

    async def _return_to(self, job: JobRecord, target: ReturnStatus) -> None:
        try:
            async with self.db.transaction(job.org_id) as conn:
                await _set_return_status(conn, job.org_id, job.return_id, target, "job_failed")
        except InvalidTransitionError as state_exc:
            # The return moved on meanwhile (e.g. a retake sent it back to capturing); the job record above
            # still carries the failure, and the return keeps its newer state.
            logger.warning("Return %s not moved to %s: %s", job.return_id, target, state_exc)
